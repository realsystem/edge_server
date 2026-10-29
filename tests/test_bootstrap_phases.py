"""Tests for the non-dry-run bootstrap phases (with a mocked SSH client)."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from bootstrap import Bootstrap, main, remote_path
from config import Config
from services import HealthStatus, ServiceHealth
from ssh import SSHResult

OK = SSHResult(0, "", "")
FAIL = SSHResult(1, "boom output", "boom")


def make(deploy_type="full", mode="auto", **kw):
    config = Config()
    config.ssh.user = "tester"
    config.rollback.snapshot_before_deploy = False
    b = Bootstrap(target="192.0.2.10", config=config, mode=mode, deploy_type=deploy_type, **kw)
    b.ssh = MagicMock()
    b.ssh.run.return_value = OK
    b.snapshots = MagicMock()
    b.checker = MagicMock()
    return b


def commands(b):
    return [c.args[0] for c in b.ssh.run.call_args_list]


class TestRemotePath:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("$HOME/.edge-server-secrets", '"$HOME"/.edge-server-secrets'),
            ("~/secrets", '"$HOME"/secrets'),
            ("$HOME", '"$HOME"'),
            ("/etc/edge server", "'/etc/edge server'"),
            ("/plain", "/plain"),
        ],
    )
    def test_remote_path(self, path, expected):
        assert remote_path(path) == expected


class TestDeploymentPhase:
    def test_security_deploy_runs_security_script(self):
        # Used to raise NameError: deploy_env/secrets_pass were only defined
        # inside the base-stack branch.
        b = make(deploy_type="security")
        assert b._phase_deployment() is True
        cmds = commands(b)
        assert len(cmds) == 1
        assert "./deploy-security.sh" in cmds[0]
        assert "deploy-edge-server.sh" not in cmds[0]

    def test_full_deploy_runs_base_only(self):
        b = make(deploy_type="full")
        assert b._phase_deployment() is True
        cmds = commands(b)
        assert len(cmds) == 1 and "./deploy-edge-server.sh" in cmds[0]

    def test_password_sent_on_stdin_not_command_line(self):
        b = make()
        b._secrets_pass = "pa$$'word"
        b.config.deploy.edge_server_dir = "/srv/edge dir"
        b._phase_deployment()
        call = b.ssh.run.call_args
        assert "pa$$'word" not in call.args[0]
        assert call.kwargs["input"] == "pa$$'word\n"
        assert "EDGE_SERVER_DIR='/srv/edge dir'" in call.args[0]
        assert 'eval "$(' in call.args[0]

    def test_failure_rolls_back_when_snapshot_taken(self):
        b = make()
        b.config.rollback.snapshot_before_deploy = True
        b.snapshots.create.return_value = "snap-id"
        b.ssh.run.return_value = FAIL
        assert b._phase_deployment() is False
        b.snapshots.restore.assert_called_once_with("latest")

    def test_failure_no_rollback_flag(self):
        b = make(no_rollback=True)
        b.config.rollback.snapshot_before_deploy = True
        b.snapshots.create.return_value = "snap-id"
        b.ssh.run.return_value = FAIL
        assert b._phase_deployment() is False
        b.snapshots.restore.assert_not_called()

    def test_rollback_disabled_skips_snapshot(self):
        b = make()
        b.config.rollback.snapshot_before_deploy = True
        b.config.rollback.enabled = False
        b._phase_deployment()
        b.snapshots.create.assert_not_called()

    def test_failed_snapshot_is_reported_not_claimed(self, capsys):
        b = make()
        b.config.rollback.snapshot_before_deploy = True
        b.snapshots.create.return_value = None
        b._phase_deployment()
        assert "Snapshot failed" in capsys.readouterr().out
        assert b._snapshot_taken is False


class TestSecretsPhase:
    def test_values_and_password_never_on_command_line(self, monkeypatch):
        monkeypatch.setenv("SECRETS_PASSWORD", "storepw")
        b = make()
        b._secrets = {}
        b.secrets_file = None
        monkeypatch.setenv("MQTT_PASS", "mq'tt;pass")
        monkeypatch.setenv("TAILSCALE_AUTH_KEY", "tskey-123")
        # [ -f secrets.enc ] -> missing, then init + sets succeed
        b.ssh.run.side_effect = [FAIL] + [OK] * 10

        assert b._phase_secrets() is True

        calls = b.ssh.run.call_args_list
        init = calls[1]
        assert init.args[0].endswith("./secrets.sh init 2>&1")
        assert init.kwargs["input"] == "storepw\nstorepw\n"
        sets = calls[2:]
        for c in sets:
            cmd = c.args[0]
            assert "storepw" not in cmd and "mq'tt" not in cmd and "tskey" not in cmd
            assert cmd.endswith(" - 2>&1")
        inputs = {c.args[0].split(" set ")[1].split()[0]: c.kwargs["input"] for c in sets}
        assert inputs["MQTT_PASS"] == "storepw\nmq'tt;pass\n"
        assert 'SECRETS_DIR="$HOME"/.edge-server-secrets' in init.args[0]

    def test_auto_mode_requires_password(self, monkeypatch):
        b = make()
        b.ssh.run.return_value = OK
        assert b._phase_secrets() is False

    def test_existing_store_not_reinitialised(self, monkeypatch):
        monkeypatch.setenv("SECRETS_PASSWORD", "pw")
        b = make()
        b._secrets = {}
        assert b._phase_secrets() is True
        assert not any("secrets.sh init" in c for c in commands(b))

    def test_set_failure_fails_phase(self, monkeypatch):
        monkeypatch.setenv("SECRETS_PASSWORD", "pw")
        monkeypatch.setenv("MQTT_PASS", "x")
        b = make()
        b.ssh.run.side_effect = [OK] + [FAIL] * 10
        assert b._phase_secrets() is False

    def test_secrets_file_parsing(self, tmp_path):
        f = tmp_path / "s.env"
        f.write_text(
            "# comment\n"
            "export MQTT_USER=alice\n"
            'MQTT_PASS="quoted value"\n'
            "REOLINK_PASS='single'\n"
            "TAILSCALE_AUTH_KEY=tskey=with=equals\n"
            "garbage line\n"
        )
        b = make(secrets_file=f)
        b._load_secrets_file()
        assert b._secrets == {
            "MQTT_USER": "alice",
            "MQTT_PASS": "quoted value",
            "REOLINK_PASS": "single",
            "TAILSCALE_AUTH_KEY": "tskey=with=equals",
        }


class TestInitialSetupPhase:
    def test_runs_non_interactively_with_network_env(self, monkeypatch):
        monkeypatch.setenv("STATIC_IP", "192.168.1.50/24")
        monkeypatch.setenv("GATEWAY", "192.168.1.1")
        b = make()
        b.ssh.run_script.return_value = OK
        b.ssh.test_connection.return_value = True
        with patch("bootstrap.time.sleep"):
            assert b._phase_initial_setup() is True
        env = b.ssh.run_script.call_args.kwargs["env"]
        assert env["BATCH_MODE"] == "true"
        assert env["STATIC_IP"] == "192.168.1.50/24"
        assert env["GATEWAY"] == "192.168.1.1"
        assert env["DNS"] == "8.8.8.8"
        b.ssh.reboot.assert_called_once()

    def test_manual_mode_still_batch_for_script(self, monkeypatch):
        b = make(mode="manual")
        b.ssh.run_script.return_value = OK
        with patch("builtins.input", side_effect=["", "n"]):
            assert b._phase_initial_setup() is True
        assert b.ssh.run_script.call_args.kwargs["env"] == {"BATCH_MODE": "true"}
        b.ssh.reboot.assert_not_called()

    def test_script_failure(self):
        b = make()
        b.ssh.run_script.return_value = FAIL
        assert b._phase_initial_setup() is False

    def test_host_never_returns(self):
        b = make()
        b.config.timeouts.reboot_wait = 0
        b.ssh.run_script.return_value = OK
        assert b._phase_initial_setup() is False


class TestPrerequisites:
    def test_copies_scripts_and_plugins(self, tmp_path):
        for name in ["initial-setup.sh", "secrets.sh"]:
            (tmp_path / name).write_text("#!/bin/sh\n")
        (tmp_path / "plugins").mkdir()
        b = make(script_dir=tmp_path)
        b.ssh.copy_files.return_value = True
        with patch("bootstrap.shutil.which", return_value="/usr/bin/x"):
            assert b._phase_prerequisites() is True
        copied = [p.name for p in b.ssh.copy_files.call_args.args[0]]
        assert copied == ["initial-setup.sh", "secrets.sh", "plugins"]
        assert b.ssh.copy_files.call_args.args[1] == "/var/tmp/edge-server-setup"

    def test_missing_tool_fails_real_run(self):
        b = make()
        with patch("bootstrap.shutil.which", return_value=None):
            assert b._phase_prerequisites() is False

    def test_copy_failure(self, tmp_path):
        b = make(script_dir=tmp_path)
        b.ssh.copy_files.return_value = False
        with patch("bootstrap.shutil.which", return_value="/usr/bin/x"):
            assert b._phase_prerequisites() is False


class TestDiscovery:
    def test_ssh_failure(self):
        b = make()
        with patch("bootstrap.SSHClient") as cls:
            cls.return_value.test_connection.return_value = False
            assert b._phase_discovery() is False

    def test_success_wires_snapshot_dir(self):
        b = make()
        b.config.rollback.snapshot_dir = "/data/snaps"
        with patch("bootstrap.SSHClient") as cls:
            ssh = cls.return_value
            ssh.test_connection.return_value = True
            ssh.get_os_info.return_value = "Ubuntu 24.04 LTS"
            ssh.test_sudo.return_value = True
            ssh.path_exists.return_value = False
            assert b._phase_discovery() is True
        assert b.snapshots.snapshot_dir == "/data/snaps"


class TestVerification:
    def test_all_healthy(self):
        b = make()
        b._secrets = {"MQTT_USER": "u", "MQTT_PASS": "p"}
        healthy = ServiceHealth("x", HealthStatus.HEALTHY, "ok", details={"extracted": "1"})
        for m in ["check_tailscale", "check_homeassistant", "check_mqtt", "check_frigate"]:
            getattr(b.checker, m).return_value = healthy
        with patch("bootstrap.time.sleep"):
            assert b._phase_verification() is True
        assert b.checker.check_mqtt.call_args.kwargs == {
            "port": 1883,
            "user": "u",
            "password": "p",
        }

    def test_unhealthy_service_fails(self):
        b = make()
        bad = ServiceHealth("x", HealthStatus.UNHEALTHY, "down")
        for m in ["check_tailscale", "check_homeassistant", "check_mqtt", "check_frigate"]:
            getattr(b.checker, m).return_value = bad
        with patch("bootstrap.time.sleep"):
            assert b._phase_verification() is False


class TestRun:
    def test_unexpected_exception_is_caught(self):
        b = make()
        with patch.object(Bootstrap, "_phase_discovery", side_effect=RuntimeError("kaboom")):
            assert b.run() is False

    def test_dry_run_end_to_end(self):
        b = make(dry_run=True)
        assert b.run() is True

    def test_log_file_is_private(self):
        b = make()
        assert oct(b.log_file.stat().st_mode & 0o777) == "0o600"


class TestMainTimeoutValidation:
    @pytest.mark.parametrize("override", ["bogus=10", "discovery=abc", "discovery", "discovery=0"])
    def test_invalid_timeout_rejected(self, override):
        argv = ["bootstrap", "--dry-run", "--timeout", override, "192.0.2.1"]
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 2

    def test_cli_flags_applied(self):
        argv = [
            "bootstrap",
            "--dry-run",
            "--auto",
            "--user",
            "ops",
            "--key",
            "/k",
            "--no-host-key-check",
            "192.0.2.1",
        ]
        captured = {}

        def fake_run(self):
            captured["config"] = self.config
            return True

        with patch.object(sys, "argv", argv), patch.object(Bootstrap, "run", fake_run):
            assert main() == 0
        cfg = captured["config"]
        assert (cfg.ssh.user, cfg.ssh.key_file, cfg.ssh.strict_host_key_checking) == (
            "ops",
            "/k",
            "no",
        )
