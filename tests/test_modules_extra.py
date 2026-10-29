"""Additional tests for config, services, secret_store and progress."""

import json
import shutil
import subprocess
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from config import Config
from progress import Progress, Status
from secret_store import SecretsManager, SecretsShellWrapper
from services import HealthStatus, ServiceChecker
from ssh import SSHResult

# ---------------------------------------------------------------- config


def test_config_save_load_roundtrip_all_sections(tmp_path):
    cfg = Config()
    cfg.timeouts.discovery = 77
    cfg.network.default_dns = "1.1.1.1"
    cfg.services.verify_frigate = False
    cfg.rollback.snapshot_dir = "/srv/snaps"
    cfg.ssh.user = "ops"
    cfg.ssh.key_file = "~/.ssh/id_100%"
    cfg.logging.log_dir = "/var/log/edge"
    cfg.secrets.secrets_dir = "/etc/edge/secrets"
    cfg.deploy.edge_server_dir = "/srv/edge"
    cfg.deploy.edge_storage_dir = "/mnt/big"
    path = tmp_path / "bootstrap.cfg"
    cfg.save(path)

    loaded = Config.load(path)
    assert loaded == cfg


def test_config_load_missing_file_gives_defaults(tmp_path):
    assert Config.load(tmp_path / "nope.cfg") == Config()


def test_example_config_loads():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "bootstrap.cfg.example"
    cfg = Config.load(example)
    assert cfg.timeouts.deployment_security == 900
    assert cfg.rollback.snapshot_dir == "/var/lib/edge-server-snapshots"


# -------------------------------------------------------------- services


class TestMQTTCheck:
    def test_remote_uses_quoted_credentials(self):
        ssh = MagicMock()
        ssh.run.return_value = SSHResult(0, "1", "")
        health = ServiceChecker(ssh).check_mqtt(user="u", password="p w'x")
        assert health.status == HealthStatus.HEALTHY
        cmd = ssh.run.call_args.args[0]
        assert "-u u -P 'p w'\"'\"'x'" in cmd
        assert "sudo -n docker exec mosquitto" in cmd  # fallback without docker group

    def test_not_authorized_means_broker_up(self):
        ssh = MagicMock()
        ssh.run.return_value = SSHResult(
            5, "Connection error: Connection Refused: not authorised.", ""
        )
        health = ServiceChecker(ssh).check_mqtt()
        assert health.status == HealthStatus.HEALTHY
        assert "authentication" in health.message

    def test_remote_down(self):
        ssh = MagicMock()
        ssh.run.return_value = SSHResult(1, "", "Error: No such container: mosquitto")
        health = ServiceChecker(ssh).check_mqtt()
        assert health.status == HealthStatus.UNHEALTHY
        assert "No such container" in health.message

    def test_local_uses_argv_not_shell(self):
        with patch("services.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "x", "")
            health = ServiceChecker().check_mqtt(host="h;rm", port=1884)
        assert health.status == HealthStatus.HEALTHY
        argv = run.call_args.args[0]
        assert isinstance(argv, list)
        assert argv[argv.index("-h") + 1] == "h;rm"
        assert "shell" not in run.call_args.kwargs


class TestDockerContainerCheck:
    @pytest.mark.parametrize(
        "out,status",
        [
            ("running:healthy", HealthStatus.HEALTHY),
            ("running:", HealthStatus.HEALTHY),
            ("running:starting", HealthStatus.STARTING),
            ("running:unhealthy", HealthStatus.UNHEALTHY),
            ("exited:", HealthStatus.UNHEALTHY),
        ],
    )
    def test_states(self, out, status):
        ssh = MagicMock()
        ssh.run.return_value = SSHResult(0, out, "")
        assert ServiceChecker(ssh).check_docker_container("frigate").status == status

    def test_missing(self):
        with patch("services.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "no such")
            health = ServiceChecker().check_docker_container("x")
        assert health.message == "Container not found"

    def test_local_timeout(self):
        with patch("services.subprocess.run", side_effect=subprocess.TimeoutExpired("d", 1)):
            assert ServiceChecker().check_docker_container("x").status == HealthStatus.UNHEALTHY


class TestTailscaleCheck:
    def test_running(self):
        ssh = MagicMock()
        ssh.run.return_value = SSHResult(
            0, json.dumps({"BackendState": "Running", "Self": {"DNSName": "edge.ts.net."}}), ""
        )
        health = ServiceChecker(ssh).check_tailscale()
        assert health.status == HealthStatus.HEALTHY
        assert health.message == "Connected as edge.ts.net"

    def test_needs_login(self):
        ssh = MagicMock()
        ssh.run.return_value = SSHResult(0, json.dumps({"BackendState": "NeedsLogin"}), "")
        assert ServiceChecker(ssh).check_tailscale().status == HealthStatus.UNHEALTHY

    def test_bad_json_and_null_self(self):
        ssh = MagicMock()
        ssh.run.return_value = SSHResult(0, "not json", "")
        assert ServiceChecker(ssh).check_tailscale().status == HealthStatus.UNKNOWN
        ssh.run.return_value = SSHResult(
            0, json.dumps({"BackendState": "Running", "Self": None}), ""
        )
        assert ServiceChecker(ssh).check_tailscale().status == HealthStatus.HEALTHY

    def test_local_not_installed(self):
        with patch("services.subprocess.run", side_effect=FileNotFoundError("tailscale")):
            assert ServiceChecker().check_tailscale().status == HealthStatus.UNKNOWN

    def test_remote_failure(self):
        ssh = MagicMock()
        ssh.run.return_value = SSHResult(1, "", "not running")
        assert ServiceChecker(ssh).check_tailscale().message == "not running"


class TestHTTPChecks:
    def _resp(self, code, body=b"{}"):
        resp = MagicMock()
        resp.getcode.return_value = code
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        return resp

    def test_homeassistant_401_is_healthy(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, BytesIO())
        with patch("services.urllib.request.urlopen", side_effect=err):
            health = ServiceChecker().check_homeassistant("h")
        assert health.status == HealthStatus.HEALTHY
        assert health.name == "homeassistant"

    def test_http_500(self):
        err = urllib.error.HTTPError("u", 500, "Server Error", {}, BytesIO())
        with patch("services.urllib.request.urlopen", side_effect=err):
            assert ServiceChecker().check_http("http://h").status == HealthStatus.UNHEALTHY

    def test_http_unexpected_code(self):
        with patch("services.urllib.request.urlopen", return_value=self._resp(204)):
            assert ServiceChecker().check_http("http://h").status == HealthStatus.UNHEALTHY

    def test_url_error(self):
        with patch("services.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            assert ServiceChecker().check_http("http://h").message == "refused"

    def test_frigate_version(self):
        with patch(
            "services.urllib.request.urlopen",
            return_value=self._resp(200, json.dumps({"version": "0.14.1"}).encode()),
        ):
            health = ServiceChecker().check_frigate("h")
        assert health.status == HealthStatus.HEALTHY
        assert health.message == "v0.14.1"

    def test_frigate_down(self):
        with patch("services.urllib.request.urlopen", side_effect=OSError("down")):
            assert ServiceChecker().check_frigate("h").status == HealthStatus.UNHEALTHY

    def test_json_path_into_non_dict(self):
        with patch("services.urllib.request.urlopen", return_value=self._resp(200, b"[1, 2]")):
            health = ServiceChecker().check_http_json("http://h", jq_path=".a.b")
        assert health.details == {"extracted": None}


# ---------------------------------------------------------- secret store


class TestSecretStoreHardening:
    def test_export_shell_is_eval_safe(self, tmp_path):
        bash = shutil.which("bash")
        if not bash:
            pytest.skip("bash not available")
        m = SecretsManager(tmp_path / "s", password="pw")
        m.init("pw")
        marker = tmp_path / "pwned"
        evil = f"'; touch {marker}; echo '$(id)"
        m.set("EVIL", evil)
        out = subprocess.run(
            [bash, "-c", m.export_shell() + '\nprintf "%s" "$EVIL"'],
            capture_output=True,
            text=True,
        )
        assert out.stdout == evil
        assert not marker.exists()

    @pytest.mark.parametrize("key", ["", "1ABC", "A B", "A=B", "A;B"])
    def test_invalid_keys(self, tmp_path, key):
        m = SecretsManager(tmp_path / "s", password="pw")
        with pytest.raises(ValueError):
            m.set(key, "v")

    def test_newline_value_rejected(self, tmp_path):
        m = SecretsManager(tmp_path / "s", password="pw")
        with pytest.raises(ValueError):
            m.set("K", "a\nb")

    def test_missing_salt_does_not_destroy_store(self, tmp_path):
        m = SecretsManager(tmp_path / "s", password="pw")
        m.init("pw")
        m.set("K", "v")
        (tmp_path / "s" / ".salt").unlink()
        before = (tmp_path / "s" / "secrets.enc").read_bytes()
        with pytest.raises(ValueError, match="Salt file missing"):
            SecretsManager(tmp_path / "s", password="pw").get("K")
        assert (tmp_path / "s" / "secrets.enc").read_bytes() == before

    def test_password_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRETS_PASSWORD", "envpw")
        m = SecretsManager(tmp_path / "s")
        m.set("K", "v")
        assert SecretsManager(tmp_path / "s", password="envpw").get("K") == "v"

    def test_no_password(self, tmp_path):
        with pytest.raises(ValueError, match="No password"):
            SecretsManager(tmp_path / "s").set("K", "v")

    def test_delete_and_clear(self, tmp_path):
        m = SecretsManager(tmp_path / "s", password="pw")
        m.set("A", "1")
        assert m.delete("A") is True
        assert m.delete("A") is False
        m.set("B", "2")
        m.clear()
        assert not (tmp_path / "s" / "secrets.enc").exists()


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not available")
def test_shell_wrapper_against_real_script(tmp_path):
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "secrets.sh"
    w = SecretsShellWrapper(script, home_dir=tmp_path)
    assert w.init("pw") is True
    assert w.set("API_KEY", "a b'c", password="pw") is True
    assert w.get("API_KEY", password="pw") == "a b'c"
    assert any("API_KEY" in k for k in w.list_keys(password="pw"))
    assert "export API_KEY=" in w.export(password="pw")
    assert w.get("API_KEY", password="wrong") is None
    assert w.list_keys(password="wrong") == []
    assert w.export(password="wrong") == ""


# --------------------------------------------------------------- progress


def test_progress_output(capsys):
    p = Progress(no_color=True)
    p.header("Title", "Auto", dry_run=True)
    p.phase(1, "Discovery", 30)
    with p.task("thing"):
        pass
    with pytest.raises(RuntimeError):
        with p.task("bad"):
            raise RuntimeError("x")
    p.task_status("manual", Status.WARNING, 1.0, "details")
    p.waiting("wait", 5, 10)
    p.waiting("wait", 5, 0)
    p.clear_line()
    p.ok("ok")
    p.warn("warn")
    p.fail("fail", "why")
    p.skip("skip")
    p.dry_run("dry")
    p.summary(1, 2, 3)
    p.footer(True, {"HA": "http://x"})
    p.footer(False)
    p.phase_summary()
    with patch("progress.time.sleep"):
        p.countdown("go", 2)
    assert p.progress_bar(5, 10, width=10) == "[█████░░░░░]"
    assert p.progress_bar(0, 0, width=4) == "[    ]"
    assert p.total_elapsed() >= 0
    out = capsys.readouterr().out
    for s in ["DRY RUN", "Phase 1: Discovery", "✓ thing", "✗ bad", "why", "Deployment failed"]:
        assert s in out
