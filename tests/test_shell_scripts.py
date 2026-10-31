"""Behavioural tests for the shell scripts (secrets.sh, deploy scripts, NUT poller).

The deploy scripts are sourced (they only run ``main`` when executed directly)
and individual functions are exercised against a temporary directory, with
system tools such as docker/systemctl/chown replaced by stubs on PATH.
"""

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
OPENSSL = shutil.which("openssl")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def run_bash(script, env=None, input=None, cwd=REPO):
    full_env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    full_env.update(env or {})
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        env=full_env,
        input=input,
        cwd=cwd,
        timeout=60,
    )


# --------------------------------------------------------------------------
# secrets.sh
# --------------------------------------------------------------------------


@pytest.mark.skipif(OPENSSL is None, reason="openssl not available")
class TestSecretsScript:
    PASSWORD = "correct horse"

    @pytest.fixture
    def store(self, tmp_path):
        secrets_dir = tmp_path / "secrets"
        env = {"SECRETS_DIR": str(secrets_dir)}
        result = run_bash("./secrets.sh init", env=env, input=f"{self.PASSWORD}\n{self.PASSWORD}\n")
        assert result.returncode == 0, result.stderr
        return secrets_dir

    def secrets(self, store, *args, input=None, password=PASSWORD):
        env = {"SECRETS_DIR": str(store)}
        if password is not None:
            env["SECRETS_PASSWORD"] = password
        quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
        return run_bash(f"./secrets.sh {quoted}", env=env, input=input)

    def test_init_creates_private_encrypted_file(self, store):
        enc = store / "secrets.enc"
        assert enc.exists()
        assert stat.S_IMODE(enc.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.stat().st_mode) == 0o700
        assert b"MQTT_USER" not in enc.read_bytes()
        # No plaintext left behind
        assert sorted(p.name for p in store.iterdir()) == ["secrets.enc"]

    def test_init_rejects_mismatched_passwords(self, tmp_path):
        env = {"SECRETS_DIR": str(tmp_path / "s")}
        result = run_bash("./secrets.sh init", env=env, input="a\nb\n")
        assert result.returncode != 0
        assert "do not match" in result.stderr + result.stdout

    @pytest.mark.parametrize(
        "value",
        [
            "simple",
            "with spaces  and\ttabs",
            "sed|meta&chars/and\\backslash",
            "quote'single\"double",
            "$(touch /tmp/edge-pwned) `id` $HOME",
            "=leading=equals=",
        ],
    )
    def test_set_get_roundtrip_special_characters(self, store, value):
        assert self.secrets(store, "set", "MY_KEY", value).returncode == 0
        result = self.secrets(store, "get", "MY_KEY")
        assert result.returncode == 0
        assert result.stdout.rstrip("\n") == value

    def test_set_replaces_existing_key(self, store):
        self.secrets(store, "set", "MQTT_PASS", "first")
        self.secrets(store, "set", "MQTT_PASS", "second")
        assert self.secrets(store, "get", "MQTT_PASS").stdout.strip() == "second"
        listing = self.secrets(store, "list").stdout
        assert listing.count("MQTT_PASS") == 1

    def test_set_value_from_stdin(self, store):
        # password then value, both on stdin (what bootstrap does over SSH)
        result = self.secrets(
            store, "set", "TOKEN", "-", input=f"{self.PASSWORD}\nfrom stdin\n", password=None
        )
        assert result.returncode == 0, result.stderr
        assert self.secrets(store, "get", "TOKEN").stdout.strip() == "from stdin"

    def test_invalid_key_rejected(self, store):
        result = self.secrets(store, "set", "BAD KEY", "x")
        assert result.returncode != 0
        assert "Invalid key" in result.stderr

    def test_wrong_password_fails_without_output(self, store):
        result = self.secrets(store, "get", "MQTT_USER", password="wrong")
        assert result.returncode != 0
        assert result.stdout == ""

    def test_missing_key_returns_empty(self, store):
        result = self.secrets(store, "get", "NOPE")
        assert result.returncode == 0
        assert result.stdout == ""

    def test_export_is_safe_to_eval(self, store, tmp_path):
        marker = tmp_path / "pwned"
        evil = f"x$(touch {marker})`touch {marker}`'\"; touch {marker}"
        self.secrets(store, "set", "EVIL", evil)
        self.secrets(store, "set", "SPACED", "a b  c")
        script = (
            'eval "$(./secrets.sh export)" && ' 'printf "%s\\n" "$EVIL" && printf "%s\\n" "$SPACED"'
        )
        result = run_bash(
            script, env={"SECRETS_DIR": str(store), "SECRETS_PASSWORD": self.PASSWORD}
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [evil, "a b  c"]
        assert not marker.exists()

    def test_uninitialized_store_errors(self, tmp_path):
        env = {"SECRETS_DIR": str(tmp_path / "none"), "SECRETS_PASSWORD": "x"}
        result = run_bash("./secrets.sh get A", env=env)
        assert result.returncode != 0
        assert "not initialized" in result.stderr

    def test_help(self):
        result = run_bash("./secrets.sh --help")
        assert result.returncode == 0
        assert "Usage" in result.stdout

    def test_unknown_command(self):
        result = run_bash("./secrets.sh frobnicate")
        assert result.returncode != 0


# --------------------------------------------------------------------------
# NUT battery poller
# --------------------------------------------------------------------------


class TestBatteryPoller:
    POLLER = REPO / "plugins" / "nut-power" / "battery-poller.sh"

    def poller(self, body, env=None):
        return run_bash(f"source {self.POLLER}\n{body}", env=env)

    @pytest.mark.parametrize(
        "capacity,ac,on_batt,expected",
        [
            (90, "1", 0, "OL"),
            (90, "0", 10, "OB"),
            (15, "1", 0, "OL LB"),  # low battery even on AC
            (15, "0", 0, "OB LB"),
            (90, "0", 600, "OB LB"),  # on battery for the full delay -> shutdown
            (90, "0", 599, "OB"),
        ],
    )
    def test_compute_status(self, capacity, ac, on_batt, expected):
        result = self.poller(
            f"compute_status {capacity} {ac} {on_batt}",
            env={"NUT_LOW_BATTERY": "20", "NUT_SHUTDOWN_DELAY": "600"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected

    def test_delay_zero_disables_timed_shutdown(self):
        result = self.poller("compute_status 90 0 99999", env={"NUT_SHUTDOWN_DELAY": "0"})
        assert result.stdout.strip() == "OB"

    def test_reads_sysfs(self, tmp_path):
        bat = tmp_path / "BAT0"
        ac = tmp_path / "AC"
        bat.mkdir()
        ac.mkdir()
        (bat / "type").write_text("Battery\n")
        (bat / "capacity").write_text("42\n")
        (ac / "type").write_text("Mains\n")
        (ac / "online").write_text("0\n")
        result = self.poller(
            "get_battery_capacity; get_ac_online",
            env={"POWER_SUPPLY_DIR": str(tmp_path)},
        )
        assert result.stdout.split() == ["42", "0"]

    def test_no_battery_defaults(self, tmp_path):
        result = self.poller(
            "get_battery_capacity; get_ac_online", env={"POWER_SUPPLY_DIR": str(tmp_path)}
        )
        assert result.stdout.split() == ["100", "1"]

    def test_write_nut_status(self, tmp_path):
        dummy = tmp_path / "laptop.dev"
        result = self.poller(
            "write_nut_status 55 0 30",
            env={"NUT_DUMMY_FILE": str(dummy), "NUT_LOW_BATTERY": "20"},
        )
        assert result.returncode == 0, result.stderr
        content = dummy.read_text()
        assert "battery.charge: 55" in content
        assert "ups.status: OB\n" in content
        assert "battery.charge.low: 20" in content
        assert not (tmp_path / "laptop.dev.tmp").exists()

    def test_nut_config_has_no_fake_directives(self):
        conf = (REPO / "plugins/nut-power/config/upsmon.conf").read_text()
        directives = [
            line.split()[0] for line in conf.splitlines() if line.strip() and line[0] != "#"
        ]
        assert "ONBATTERYDELAY" not in directives
        assert "edgeserver" not in conf  # no hard-coded password


# --------------------------------------------------------------------------
# Deploy scripts (sourced, functions run against a temp dir)
# --------------------------------------------------------------------------

STUBS = {
    # docker: emulate `docker run ... -v HOST:/mosquitto/config ... mosquitto_passwd`
    "docker": textwrap.dedent("""\
        #!/usr/bin/env bash
        echo "docker $*" >> "$STUB_LOG"
        prev=""
        for a in "$@"; do
            if [[ "$prev" == "-v" && "$a" == *:/mosquitto/config ]]; then
                printf 'user:hash\\n' > "${a%%:*}/passwd"
            fi
            prev="$a"
        done
        exit 0
        """),
    "chown": "#!/usr/bin/env bash\nexit 0\n",
    "systemctl": '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\nexit 0\n',
}


@pytest.fixture
def stub_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    return bin_dir


def run_sourced(script, body, stub_path, env):
    prelude = (
        f"source {REPO / script}\n"
        # keep logging out of /var/log and /tmp
        "log() { printf '%s\\n' \"$*\"; }\n"
        "info() { printf '%s\\n' \"$*\"; }; warn() { printf '%s\\n' \"$*\"; }\n"
        "step() { printf '%s\\n' \"$*\"; }; error() { printf '%s\\n' \"$*\"; }\n"
    )
    full_env = {"PATH": f"{stub_path}:{os.environ['PATH']}", "STUB_LOG": str(stub_path / "log")}
    full_env.update(env)
    return run_bash(prelude + body, env=full_env)


class TestDeployEdgeServer:
    def test_sourcing_does_not_run_main(self, stub_path, tmp_path):
        result = run_sourced("deploy-edge-server.sh", "echo sourced-ok", stub_path, {})
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().endswith("sourced-ok")

    @pytest.fixture
    def stack(self, stub_path, tmp_path):
        app = tmp_path / "app"
        storage = tmp_path / "storage"
        env = {
            "EDGE_SERVER_DIR": str(app),
            "EDGE_STORAGE_DIR": str(storage),
            "MQTT_USER": "mqttuser",
            "MQTT_PASS": "s3cret#pass",
            "INSTALL_HOMEASSISTANT": "true",
            "INSTALL_MQTT": "true",
        }
        result = run_sourced("deploy-edge-server.sh", "create_app_stack", stub_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        return app, storage, result

    def test_full_stack_dependencies(self, stack):
        app, _, _ = stack
        services = yaml.safe_load((app / "docker-compose.yml").read_text())["services"]
        assert services["frigate"]["depends_on"] == ["mosquitto"]
        assert services["homeassistant"]["depends_on"] == ["mosquitto"]

    def test_compose_is_valid_and_uses_storage_dir(self, stack):
        app, storage, _ = stack
        compose = yaml.safe_load((app / "docker-compose.yml").read_text())
        assert set(compose["services"]) == {"mosquitto", "homeassistant", "frigate"}
        frigate = compose["services"]["frigate"]
        assert f"{storage}/frigate:/media/frigate" in frigate["volumes"]
        # Hardware acceleration is opt-in, so no device mapping by default
        assert "devices" not in frigate

    def test_frigate_config_valid_and_private(self, stack):
        app, _, _ = stack
        cfg_path = app / "frigate" / "config.yml"
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg["mqtt"]["user"] == "mqttuser"
        assert cfg["mqtt"]["password"] == "s3cret#pass"
        assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

    def test_ha_config_has_no_removed_yaml_integrations(self, stack):
        app, _, _ = stack
        ha = yaml.safe_load((app / "ha-config" / "configuration.yaml").read_text())
        assert "mqtt" not in ha  # `mqtt: broker:` is rejected by current HA
        assert "sensor" not in ha
        assert stat.S_IMODE((app / "ha-config").stat().st_mode) == 0o700

    def test_mosquitto_requires_auth(self, stack):
        app, _, _ = stack
        conf = (app / "mosquitto" / "config" / "mosquitto.conf").read_text()
        assert "allow_anonymous false" in conf
        passwd = app / "mosquitto" / "config" / "passwd"
        assert passwd.exists()
        assert stat.S_IMODE(passwd.stat().st_mode) == 0o600

    def test_rerun_does_not_open_up_permissions(self, stub_path, stack):
        app, storage, _ = stack
        secret = app / "ha-config" / ".storage"
        secret.mkdir()
        (secret / "auth").write_text("{}")
        (secret / "auth").chmod(0o600)
        env = {
            "EDGE_SERVER_DIR": str(app),
            "EDGE_STORAGE_DIR": str(storage),
            "MQTT_USER": "u",
            "MQTT_PASS": "p",
            "INSTALL_HOMEASSISTANT": "true",
            "INSTALL_MQTT": "true",
        }
        result = run_sourced("deploy-edge-server.sh", "create_app_stack", stub_path, env)
        assert result.returncode == 0
        assert stat.S_IMODE((secret / "auth").stat().st_mode) == 0o600

    def test_health_check_uses_configured_dirs(self, stub_path, tmp_path):
        app = tmp_path / "custom-app"
        app.mkdir()
        storage = tmp_path / "custom-storage"
        env = {"EDGE_SERVER_DIR": str(app), "EDGE_STORAGE_DIR": str(storage)}
        # Redirect the systemd unit files into the temp dir, then run the real
        # function (systemctl is stubbed).
        script = (
            f"export UNIT_DIR={tmp_path}\n"
            'eval "$(declare -f create_systemd_service '
            "| sed 's|/etc/systemd/system|'$UNIT_DIR'|g')\"\n"
            "create_systemd_service\n"
        )
        result = run_sourced("deploy-edge-server.sh", script, stub_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        health = (app / "health-check.sh").read_text()
        assert f'COMPOSE_DIR="{app}"' in health
        assert f'STORAGE_MOUNT="{storage}"' in health
        assert "/opt/edge-server" not in health
        unit = (tmp_path / "edge-server-health.service").read_text()
        assert f"ExecStart={app}/health-check.sh" in unit
        assert subprocess.run([BASH, "-n", str(app / "health-check.sh")]).returncode == 0


# --------------------------------------------------------------------------
# Re-deploy (update) must not wipe a customised Frigate config
# --------------------------------------------------------------------------

CUSTOM_FRIGATE = "# my cameras\ncameras:\n  barn_cam:\n    enabled: true\n"


@pytest.mark.parametrize(
    "script,func,extra_env",
    [
        ("deploy-edge-server.sh", "create_app_stack", {"MQTT_USER": "u", "MQTT_PASS": "p"}),
    ],
)
class TestFrigateConfigPreserved:
    def run(self, stub_path, tmp_path, script, func, extra_env, **more):
        env = {
            "EDGE_SERVER_DIR": str(tmp_path / "app"),
            "EDGE_STORAGE_DIR": str(tmp_path / "storage"),
            **extra_env,
            **more,
        }
        result = run_sourced(script, func, stub_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        return tmp_path / "app" / "frigate", result

    def seed(self, tmp_path):
        frigate = tmp_path / "app" / "frigate"
        frigate.mkdir(parents=True)
        cfg = frigate / "config.yml"
        cfg.write_text(CUSTOM_FRIGATE)
        cfg.chmod(0o644)
        return cfg

    def test_fresh_install_creates_config(self, stub_path, tmp_path, script, func, extra_env):
        frigate, result = self.run(stub_path, tmp_path, script, func, extra_env)
        assert yaml.safe_load((frigate / "config.yml").read_text())["cameras"]
        assert not (frigate / "config.yml.new").exists()
        assert "Created Frigate config" in result.stdout

    def test_redeploy_keeps_existing_config(self, stub_path, tmp_path, script, func, extra_env):
        cfg = self.seed(tmp_path)
        frigate, result = self.run(stub_path, tmp_path, script, func, extra_env)
        assert cfg.read_text() == CUSTOM_FRIGATE
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
        new = frigate / "config.yml.new"
        assert "mqtt" in yaml.safe_load(new.read_text())
        assert stat.S_IMODE(new.stat().st_mode) == 0o600
        assert "Kept existing Frigate config" in result.stdout

    def test_overwrite_flag_regenerates_with_backup(
        self, stub_path, tmp_path, script, func, extra_env
    ):
        cfg = self.seed(tmp_path)
        frigate, _ = self.run(
            stub_path, tmp_path, script, func, extra_env, FRIGATE_CONFIG_OVERWRITE="true"
        )
        assert cfg.read_text() != CUSTOM_FRIGATE
        backups = list(frigate.glob("config.yml.bak.*"))
        assert len(backups) == 1 and backups[0].read_text() == CUSTOM_FRIGATE
        assert not (frigate / "config.yml.new").exists()

    def test_replaces_empty_dir_left_by_docker(self, stub_path, tmp_path, script, func, extra_env):
        (tmp_path / "app" / "frigate" / "config.yml").mkdir(parents=True)
        frigate, _ = self.run(stub_path, tmp_path, script, func, extra_env)
        assert (frigate / "config.yml").is_file()


# --------------------------------------------------------------------------
# Frigate-only default (Home Assistant and Mosquitto off)
# --------------------------------------------------------------------------


class TestFrigateOnlyDefault:
    def run(self, stub_path, tmp_path, body="create_app_stack", **env):
        full_env = {
            "EDGE_SERVER_DIR": str(tmp_path / "app"),
            "EDGE_STORAGE_DIR": str(tmp_path / "storage"),
            **env,
        }
        result = run_sourced("deploy-edge-server.sh", body, stub_path, full_env)
        assert result.returncode == 0, result.stdout + result.stderr
        return tmp_path / "app", result

    def test_only_frigate_in_compose(self, stub_path, tmp_path):
        app, _ = self.run(stub_path, tmp_path)
        services = yaml.safe_load((app / "docker-compose.yml").read_text())["services"]
        assert set(services) == {"frigate"}
        assert "depends_on" not in services["frigate"]
        assert not (app / "mosquitto").exists()
        assert not (app / "ha-config").exists()

    def test_fresh_frigate_config_has_mqtt_off(self, stub_path, tmp_path):
        app, _ = self.run(stub_path, tmp_path)
        cfg = yaml.safe_load((app / "frigate" / "config.yml").read_text())
        assert cfg["mqtt"] == {"enabled": False}

    def test_existing_config_mqtt_turned_off_with_backup(self, stub_path, tmp_path):
        frigate = tmp_path / "app" / "frigate"
        frigate.mkdir(parents=True)
        cfg = frigate / "config.yml"
        original = (
            "mqtt:\n  enabled: true\n  host: mosquitto\n  password: x\n"
            "cameras:\n  barn:\n    enabled: true\n"
        )
        cfg.write_text(original)
        _, result = self.run(stub_path, tmp_path)
        data = yaml.safe_load(cfg.read_text())
        assert data["mqtt"]["enabled"] is False
        assert data["cameras"] == {"barn": {"enabled": True}}  # cameras untouched
        backups = list(frigate.glob("config.yml.bak.*"))
        assert len(backups) == 1 and backups[0].read_text() == original
        assert "Turned off MQTT" in result.stdout

    def test_existing_config_already_off_untouched(self, stub_path, tmp_path):
        frigate = tmp_path / "app" / "frigate"
        frigate.mkdir(parents=True)
        cfg = frigate / "config.yml"
        cfg.write_text("mqtt:\n  enabled: false\ncameras: {}\n")
        self.run(stub_path, tmp_path)
        assert cfg.read_text() == "mqtt:\n  enabled: false\ncameras: {}\n"
        assert not list(frigate.glob("config.yml.bak.*"))

    def test_unrecognised_mqtt_layout_warns_only(self, stub_path, tmp_path):
        frigate = tmp_path / "app" / "frigate"
        frigate.mkdir(parents=True)
        cfg = frigate / "config.yml"
        cfg.write_text("mqtt: {host: broker}\n")
        _, result = self.run(stub_path, tmp_path)
        assert cfg.read_text() == "mqtt: {host: broker}\n"
        assert "set 'mqtt: enabled: false'" in result.stdout

    def test_firewall_only_opens_frigate(self, stub_path, tmp_path):
        ufw = stub_path / "ufw"
        ufw.write_text('#!/usr/bin/env bash\necho "ufw $*" >> "$STUB_LOG"\n')
        ufw.chmod(0o755)
        self.run(stub_path, tmp_path, body="configure_firewall")
        log = (stub_path / "log").read_text()
        assert "port 5000" in log
        assert "port 1883" not in log and "port 8123" not in log

    def test_firewall_with_optional_services(self, stub_path, tmp_path):
        ufw = stub_path / "ufw"
        ufw.write_text('#!/usr/bin/env bash\necho "ufw $*" >> "$STUB_LOG"\n')
        ufw.chmod(0o755)
        self.run(
            stub_path,
            tmp_path,
            body="configure_firewall",
            INSTALL_MQTT="true",
            INSTALL_HOMEASSISTANT="true",
        )
        log = (stub_path / "log").read_text()
        assert "port 1883" in log and "port 8123" in log

    @pytest.mark.parametrize(
        "env,expected",
        [
            ({}, "CONTAINERS=(frigate)"),
            (
                {"INSTALL_MQTT": "true", "INSTALL_HOMEASSISTANT": "true"},
                "CONTAINERS=(frigate mosquitto homeassistant)",
            ),
            ({"INSTALL_TELEGRAM_BOT": "true"}, "CONTAINERS=(frigate telegram-bot)"),
        ],
    )
    def test_health_check_watches_installed_containers(self, stub_path, tmp_path, env, expected):
        (tmp_path / "app").mkdir()
        script = (
            f"export UNIT_DIR={tmp_path}\n"
            'eval "$(declare -f create_systemd_service '
            "| sed 's|/etc/systemd/system|'$UNIT_DIR'|g')\"\n"
            "create_systemd_service\n"
        )
        app, _ = self.run(stub_path, tmp_path, body=script, **env)
        assert expected in (app / "health-check.sh").read_text()

    def test_start_services_removes_orphans_and_restarts_on_change(self, stub_path, tmp_path):
        frigate = tmp_path / "app" / "frigate"
        frigate.mkdir(parents=True)
        (frigate / "config.yml").write_text("mqtt:\n  enabled: true\n")
        body = "create_app_stack\nsleep() { :; }\nstart_services"
        self.run(stub_path, tmp_path, body=body)
        log = (stub_path / "log").read_text()
        assert "docker compose up -d --remove-orphans" in log
        assert "docker compose restart frigate" in log

    def test_start_services_no_restart_when_unchanged(self, stub_path, tmp_path):
        frigate = tmp_path / "app" / "frigate"
        frigate.mkdir(parents=True)
        (frigate / "config.yml").write_text("mqtt:\n  enabled: false\n")
        body = "create_app_stack\nsleep() { :; }\nstart_services"
        self.run(stub_path, tmp_path, body=body)
        assert "restart frigate" not in (stub_path / "log").read_text()

    def test_no_mqtt_password_prompt_by_default(self, stub_path, tmp_path):
        # preflight would block on `read` if it still asked for an MQTT password
        body = (
            "curl() { :; }; df() { printf 'h\\n/ 1 2 50G 1%% /\\n'; }\n"
            "fatal() { :; }  # root/OS checks are not under test here\n"
            "BATCH_MODE=false EXTERNAL_DRIVE_UUID=x TAILSCALE_AUTH_KEY=x preflight_checks"
        )
        _, result = self.run(stub_path, tmp_path, body=body)
        assert "MQTT password" not in result.stdout


# --------------------------------------------------------------------------
# Camera discovery wiring
# --------------------------------------------------------------------------


@pytest.fixture
def fake_python(stub_path):
    """python3 stub that records its arguments instead of probing the network."""
    py = stub_path / "python3"
    py.write_text('#!/usr/bin/env bash\necho "python3 $*" >> "$STUB_LOG"\n')
    py.chmod(0o755)
    return stub_path / "log"


class TestDiscoverCamerasInDeploy:
    def run(self, stub_path, tmp_path, **env):
        app = tmp_path / "app"
        (app / "frigate").mkdir(parents=True, exist_ok=True)
        full_env = {"EDGE_SERVER_DIR": str(app), **env}
        result = run_sourced("deploy-edge-server.sh", "discover_cameras", stub_path, full_env)
        assert result.returncode == 0, result.stdout + result.stderr
        return app, result

    def test_skipped_without_cam_file(self, stub_path, tmp_path, fake_python):
        _, result = self.run(stub_path, tmp_path, CAM_CREDENTIALS_FILE=str(tmp_path / "none"))
        assert "skipping camera discovery" in result.stdout
        assert not fake_python.exists()

    def test_runs_tool_with_private_copy(self, stub_path, tmp_path, fake_python):
        cam = tmp_path / "my.cam"
        cam.write_text("admin:secret\n")
        cam.chmod(0o644)
        app, _ = self.run(stub_path, tmp_path, CAM_CREDENTIALS_FILE=str(cam))
        copy = app / ".cam"
        assert copy.read_text() == "admin:secret\n"
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
        call = fake_python.read_text()
        assert "tools/camera_discovery.py" in call
        assert f"--credentials {copy}" in call
        assert f"--config {app}/frigate/config.yml --apply" in call

    def test_tool_failure_is_a_warning(self, stub_path, tmp_path):
        py = stub_path / "python3"
        py.write_text("#!/usr/bin/env bash\nexit 1\n")
        py.chmod(0o755)
        cam = tmp_path / "my.cam"
        cam.write_text("a:b\n")
        _, result = self.run(stub_path, tmp_path, CAM_CREDENTIALS_FILE=str(cam))
        assert "add cameras to" in result.stdout

    def test_deploy_main_runs_discovery_before_start(self):
        text = (REPO / "deploy-edge-server.sh").read_text()
        main_body = text[text.index("main() {") :]
        assert main_body.index("discover_cameras") < main_body.index("start_services")


class TestDiscoverCamerasWrapper:
    WRAPPER = REPO / "discover-cameras.sh"

    @pytest.mark.skipif(os.geteuid() == 0, reason="checks the non-root refusal")
    def test_requires_root(self):
        result = run_bash(str(self.WRAPPER))
        assert result.returncode == 1 and "sudo" in result.stderr

    @pytest.mark.skipif(os.geteuid() != 0, reason="wrapper only runs as root")
    def test_restarts_frigate_only_when_config_changes(self, stub_path, tmp_path):
        app = tmp_path / "app"
        (app / "frigate").mkdir(parents=True)
        cfg = app / "frigate" / "config.yml"
        cfg.write_text("cameras:\n")
        cam = tmp_path / "x.cam"
        cam.write_text("a:b\n")
        py = stub_path / "python3"
        env = {
            "PATH": f"{stub_path}:{os.environ['PATH']}",
            "STUB_LOG": str(stub_path / "log"),
            "EDGE_SERVER_DIR": str(app),
            "CAM_CREDENTIALS_FILE": str(cam),
        }
        # 1) tool changes nothing -> no restart
        py.write_text('#!/usr/bin/env bash\necho "python3 $*" >> "$STUB_LOG"\n')
        py.chmod(0o755)
        result = run_bash(f"{self.WRAPPER} --dry-run --host 10.0.0.5", env=env)
        assert result.returncode == 0, result.stderr
        log = (stub_path / "log").read_text()
        assert "--apply" not in log and "--host 10.0.0.5" in log
        assert "restart frigate" not in log
        # 2) tool adds a camera -> Frigate restarted
        py.write_text(f'#!/usr/bin/env bash\necho "  new: {{}}" >> {cfg}\n')
        result = run_bash(str(self.WRAPPER), env=env)
        assert result.returncode == 0, result.stderr
        assert "docker compose restart frigate" in (stub_path / "log").read_text()

    @pytest.mark.skipif(os.geteuid() != 0, reason="wrapper only runs as root")
    def test_missing_cam_file(self, tmp_path):
        env = {"EDGE_SERVER_DIR": str(tmp_path), "CAM_CREDENTIALS_FILE": str(tmp_path / "nope")}
        result = run_bash(f"cd {tmp_path} && {self.WRAPPER}", env=env)
        if (REPO / ".cam").exists():
            pytest.skip("a real .cam exists in the repo checkout")
        assert result.returncode == 1 and "No .cam file" in result.stderr


# --------------------------------------------------------------------------
# Current Frigate rejects `motion: mask: []` (masks are now a dictionary)
# --------------------------------------------------------------------------


def _has_empty_mask(node):
    if isinstance(node, dict):
        if node.get("mask") == []:
            return True
        return any(_has_empty_mask(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_empty_mask(v) for v in node)
    return False


class TestFrigateMaskFormat:
    @pytest.mark.parametrize(
        "script,func,env",
        [
            ("deploy-edge-server.sh", "create_app_stack", {}),
        ],
    )
    def test_generated_config_has_no_empty_mask(self, stub_path, tmp_path, script, func, env):
        full_env = {"EDGE_SERVER_DIR": str(tmp_path / "app"), **env}
        result = run_sourced(script, func, stub_path, full_env)
        assert result.returncode == 0, result.stdout + result.stderr
        cfg = yaml.safe_load((tmp_path / "app" / "frigate" / "config.yml").read_text())
        assert not _has_empty_mask(cfg)

    @pytest.mark.parametrize(
        "script,func,env",
        [
            ("deploy-edge-server.sh", "create_app_stack", {}),
        ],
    )
    def test_no_null_sections(self, stub_path, tmp_path, script, func, env):
        # e.g. `ffmpeg:` with only a comment under it (no GPU) is null and
        # crashes Frigate's config loader
        full_env = {"EDGE_SERVER_DIR": str(tmp_path / "app"), **env}
        result = run_sourced(script, func, stub_path, full_env)
        assert result.returncode == 0, result.stdout + result.stderr
        cfg = yaml.safe_load((tmp_path / "app" / "frigate" / "config.yml").read_text())
        nulls = [k for k, v in cfg.items() if v is None]
        assert nulls == []
        assert "ffmpeg" not in cfg  # hwaccel is opt-in

    def test_kept_config_repaired_with_backup(self, stub_path, tmp_path):
        frigate = tmp_path / "app" / "frigate"
        frigate.mkdir(parents=True)
        cfg = frigate / "config.yml"
        old = (
            "mqtt:\n  enabled: false\n"
            "cameras:\n"
            "  front:\n    enabled: true\n    motion:\n      mask: []\n"
            "  back:\n    enabled: true\n    motion:\n      mask:\n"
            "        m1:\n          coordinates: '0,0,1,0,1,1'\n"
        )
        cfg.write_text(old)
        result = run_sourced(
            "deploy-edge-server.sh",
            "create_app_stack",
            stub_path,
            {"EDGE_SERVER_DIR": str(tmp_path / "app")},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        data = yaml.safe_load(cfg.read_text())
        assert "motion" not in data["cameras"]["front"]
        assert data["cameras"]["back"]["motion"]["mask"]["m1"]  # real masks kept
        assert any(b.read_text() == old for b in frigate.glob("config.yml.bak.*"))
        assert "no longer accepts" in result.stdout

    def test_valid_config_not_touched(self, stub_path, tmp_path):
        frigate = tmp_path / "app" / "frigate"
        frigate.mkdir(parents=True)
        cfg = frigate / "config.yml"
        cfg.write_text("mqtt:\n  enabled: false\ncameras:\n  a:\n    enabled: true\n")
        run_sourced(
            "deploy-edge-server.sh",
            "create_app_stack",
            stub_path,
            {"EDGE_SERVER_DIR": str(tmp_path / "app")},
        )
        assert cfg.read_text() == "mqtt:\n  enabled: false\ncameras:\n  a:\n    enabled: true\n"
        assert not list(frigate.glob("config.yml.bak.*"))


# --------------------------------------------------------------------------
# Frigate must be able to write its config dir (UI "Save", database)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script,func,env",
    [
        ("deploy-edge-server.sh", "create_app_stack", {}),
    ],
)
def test_frigate_config_dir_mounted_writable(stub_path, tmp_path, script, func, env):
    full_env = {"EDGE_SERVER_DIR": str(tmp_path / "app"), **env}
    result = run_sourced(script, func, stub_path, full_env)
    assert result.returncode == 0, result.stdout + result.stderr
    compose = yaml.safe_load((tmp_path / "app" / "docker-compose.yml").read_text())
    volumes = compose["services"]["frigate"]["volumes"]
    config_mounts = [v for v in volumes if isinstance(v, str) and ":/config" in v]
    assert config_mounts == ["./frigate:/config"]


# --------------------------------------------------------------------------
# Hardware acceleration is opt-in
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script,func,env",
    [
        ("deploy-edge-server.sh", "create_app_stack", {}),
    ],
)
def test_hwaccel_enabled_only_on_request(stub_path, tmp_path, script, func, env):
    """With a GPU present, ENABLE_HWACCEL=true maps it and turns on VA-API."""
    fake_gpu = tmp_path / "renderD128"
    fake_gpu.touch()
    full_env = {
        "EDGE_SERVER_DIR": str(tmp_path / "app"),
        "ENABLE_HWACCEL": "true",
        "DRI_DEVICE": str(fake_gpu),
        **env,
    }
    result = run_sourced(script, func, stub_path, full_env)
    assert result.returncode == 0, result.stdout + result.stderr
    compose = yaml.safe_load((tmp_path / "app" / "docker-compose.yml").read_text())
    assert compose["services"]["frigate"]["devices"]
    cfg = yaml.safe_load((tmp_path / "app" / "frigate" / "config.yml").read_text())
    assert cfg["ffmpeg"]["hwaccel_args"] == "preset-vaapi"


# --------------------------------------------------------------------------
# Telegram bot
# --------------------------------------------------------------------------

BOT_TOKEN = "123456789:AAH" + "x" * 30


class TestTelegramBotInDeploy:
    def run(self, stub_path, tmp_path, body="prepare_telegram_bot", telegram=None, **env):
        """Run with a .telegram file at tmp_path/.telegram (content from ``telegram``)."""
        app = tmp_path / "app"
        app.mkdir(exist_ok=True)
        full_env = {
            "EDGE_SERVER_DIR": str(app),
            "EDGE_STORAGE_DIR": str(tmp_path / "storage"),
            "TELEGRAM_CREDENTIALS_FILE": str(tmp_path / ".telegram"),
            **env,
        }
        if telegram is not None:
            (tmp_path / ".telegram").write_text(telegram)
            (tmp_path / ".telegram").chmod(0o644)
        result = run_sourced("deploy-edge-server.sh", body, stub_path, full_env)
        assert result.returncode == 0, result.stdout + result.stderr
        return app, result

    GOOD = f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_ALLOWED_IDS=1001,2002\n"

    @staticmethod
    def compose(app):
        return yaml.safe_load((app / "docker-compose.yml").read_text())

    @pytest.fixture(autouse=True)
    def _no_real_dotfile(self):
        if (REPO / ".telegram").exists():
            pytest.skip("a real .telegram exists in the repo checkout")

    def test_skipped_without_file(self, stub_path, tmp_path):
        app, result = self.run(
            stub_path, tmp_path, body="prepare_telegram_bot; echo V=$INSTALL_TELEGRAM_BOT"
        )
        assert "skipping the Telegram bot" in result.stdout and "V=false" in result.stdout
        assert not (app / "telegram-bot").exists()

    def test_installs_files_privately_and_builds_image(self, stub_path, tmp_path):
        body = "prepare_telegram_bot; echo V=$INSTALL_TELEGRAM_BOT"
        app, result = self.run(stub_path, tmp_path, body=body, telegram=self.GOOD)
        assert "V=true" in result.stdout
        assert (app / "telegram-bot/bot.py").read_text() == (
            REPO / "telegram-bot/bot.py"
        ).read_text()
        assert (app / "telegram-bot/Dockerfile").is_file()
        script = app / "telegram-bot/collect-diagnostics.sh"
        assert os.access(script, os.X_OK)
        token_file = app / ".telegram"
        assert token_file.read_text() == self.GOOD
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert (
            f"docker build -q -t edge-telegram-bot:local {app}/telegram-bot"
            in (stub_path / "log").read_text()
        )

    def test_compose_service_is_hardened_and_has_no_token(self, stub_path, tmp_path):
        body = "prepare_telegram_bot; create_app_stack"
        app, _ = self.run(stub_path, tmp_path, body=body, telegram=self.GOOD)
        compose = self.compose(app)
        bot = compose["services"]["telegram-bot"]
        assert set(compose["services"]) == {"frigate", "telegram-bot"}
        assert bot["image"] == "edge-telegram-bot:local" and bot["pull_policy"] == "never"
        assert bot["read_only"] is True and bot["pid"] == "host" and bot["init"] is True
        assert bot["cap_drop"] == ["ALL"] and "no-new-privileges:true" in bot["security_opt"]
        assert bot["env_file"] == "./.telegram"
        assert "ports" not in bot and "privileged" not in bot and "devices" not in bot
        # host paths are mounted read-only
        assert all(v.endswith(":ro") for v in bot["volumes"])
        assert any(v.startswith(f"{tmp_path / 'storage'}/frigate:") for v in bot["volumes"])
        assert BOT_TOKEN not in (app / "docker-compose.yml").read_text()

    def test_no_bot_in_compose_by_default(self, stub_path, tmp_path):
        app, _ = self.run(stub_path, tmp_path, body="create_app_stack")
        assert set(self.compose(app)["services"]) == {"frigate"}

    def test_disabled_by_flag(self, stub_path, tmp_path):
        app, result = self.run(
            stub_path, tmp_path, telegram=self.GOOD, INSTALL_TELEGRAM_BOT="false"
        )
        assert "disabled" in result.stdout
        assert not (app / "telegram-bot").exists()

    @pytest.mark.parametrize(
        "content,message",
        [
            ("TELEGRAM_BOT_TOKEN=nope\nTELEGRAM_ALLOWED_IDS=1\n", "not a bot token"),
            ("TELEGRAM_ALLOWED_IDS=1\n", "not a bot token"),
            (f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_ALLOWED_IDS=12,@bob\n", "numeric user ids"),
        ],
    )
    def test_invalid_file_skips_bot_but_not_deploy(self, stub_path, tmp_path, content, message):
        app, result = self.run(
            stub_path,
            tmp_path,
            body="prepare_telegram_bot; echo V=$INSTALL_TELEGRAM_BOT",
            telegram=content,
        )
        assert message in result.stdout and "V=false" in result.stdout
        assert not (app / "telegram-bot").exists()
        log = stub_path / "log"
        assert not log.exists() or "docker build" not in log.read_text()

    def test_empty_ids_warns_but_installs(self, stub_path, tmp_path):
        content = f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_ALLOWED_IDS=\n"
        _, result = self.run(
            stub_path,
            tmp_path,
            body="prepare_telegram_bot; echo V=$INSTALL_TELEGRAM_BOT",
            telegram=content,
        )
        assert "Send /id" in result.stdout and "V=true" in result.stdout

    def test_crlf_file_is_accepted(self, stub_path, tmp_path):
        content = f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\r\nTELEGRAM_ALLOWED_IDS=1001\r\n"
        _, result = self.run(
            stub_path,
            tmp_path,
            body="prepare_telegram_bot; echo V=$INSTALL_TELEGRAM_BOT",
            telegram=content,
        )
        assert "V=true" in result.stdout

    def failing_build(self, stub_path, image_exists):
        docker = stub_path / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            'echo "docker $*" >> "$STUB_LOG"\n'
            '[[ "$1" == build ]] && exit 1\n'
            f'[[ "$1 $2" == "image inspect" ]] && exit {0 if image_exists else 1}\n'
            "exit 0\n"
        )
        docker.chmod(0o755)

    def test_build_failure_skips_bot(self, stub_path, tmp_path):
        self.failing_build(stub_path, image_exists=False)
        _, result = self.run(
            stub_path,
            tmp_path,
            body="prepare_telegram_bot; echo V=$INSTALL_TELEGRAM_BOT",
            telegram=self.GOOD,
        )
        assert "Could not build" in result.stdout and "V=false" in result.stdout

    def test_build_failure_keeps_existing_image(self, stub_path, tmp_path):
        self.failing_build(stub_path, image_exists=True)
        _, result = self.run(
            stub_path,
            tmp_path,
            body="prepare_telegram_bot; echo V=$INSTALL_TELEGRAM_BOT",
            telegram=self.GOOD,
        )
        assert "keeping the existing one" in result.stdout and "V=true" in result.stdout

    def test_change_detection_on_redeploy(self, stub_path, tmp_path):
        body = "prepare_telegram_bot; echo C1=$TELEGRAM_CHANGED; prepare_telegram_bot; echo C2=$TELEGRAM_CHANGED"
        _, result = self.run(stub_path, tmp_path, body=body, telegram=self.GOOD)
        assert "C1=true" in result.stdout and "C2=false" in result.stdout

    # ---- /reboot (TOTP) ----

    SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    WITH_SECRET = GOOD + f"REBOOT_TOTP_SECRET={SECRET}\n"
    REBOOT_BODY = "prepare_telegram_bot; echo R=$TELEGRAM_REBOOT_ENABLED"

    def test_reboot_off_without_secret(self, stub_path, tmp_path):
        app, result = self.run(stub_path, tmp_path, body=self.REBOOT_BODY, telegram=self.GOOD)
        assert "R=false" in result.stdout
        assert not (app / "bot-requests").exists()

    def test_reboot_on_with_secret_makes_private_drop_box(self, stub_path, tmp_path):
        app, result = self.run(
            stub_path, tmp_path, body=self.REBOOT_BODY, telegram=self.WITH_SECRET
        )
        assert "R=true" in result.stdout
        assert stat.S_IMODE((app / "bot-requests").stat().st_mode) == 0o700

    @pytest.mark.parametrize(
        "value",
        [
            "gezd gnbv-gy3t qojq gezd gnbv gy3t qojq",
            "gezdgnbvgy3tqojqgezdgnbvgy3tqojq====",
        ],
    )
    def test_reboot_secret_is_normalized(self, stub_path, tmp_path, value):
        _, result = self.run(
            stub_path,
            tmp_path,
            body=self.REBOOT_BODY,
            telegram=self.GOOD + f"REBOOT_TOTP_SECRET={value}\n",
        )
        assert "R=true" in result.stdout

    @pytest.mark.parametrize("value", ["short", "GEZDGNBVGY3TQOJ1GEZDGNBV", "ABC!ABC!ABC!ABC!ABC!"])
    def test_bad_secret_skips_the_bot(self, stub_path, tmp_path, value):
        app, result = self.run(
            stub_path,
            tmp_path,
            body="prepare_telegram_bot; echo V=$INSTALL_TELEGRAM_BOT R=$TELEGRAM_REBOOT_ENABLED",
            telegram=self.GOOD + f"REBOOT_TOTP_SECRET={value}\n",
        )
        assert "REBOOT_TOTP_SECRET must be base32" in result.stdout
        assert "V=false R=false" in result.stdout
        assert not (app / "telegram-bot").exists()

    def test_compose_gets_the_requests_mount_only_when_enabled(self, stub_path, tmp_path):
        body = "prepare_telegram_bot; create_app_stack"
        app, _ = self.run(stub_path, tmp_path, body=body, telegram=self.WITH_SECRET)
        bot = self.compose(app)["services"]["telegram-bot"]
        assert "./bot-requests:/requests" in bot["volumes"]
        assert "REQUESTS_DIR=/requests" in bot["environment"]
        rw = [v for v in bot["volumes"] if not v.endswith(":ro")]
        assert rw == ["./bot-requests:/requests"]
        assert bot["read_only"] is True and bot["cap_drop"] == ["ALL"]
        assert self.SECRET not in (app / "docker-compose.yml").read_text()

        other = tmp_path / "plain"
        other.mkdir()
        app2, _ = self.run(stub_path, other, body=body, telegram=self.GOOD)
        bot2 = self.compose(app2)["services"]["telegram-bot"]
        assert all(v.endswith(":ro") for v in bot2["volumes"])
        assert not any("REQUESTS_DIR" in e for e in bot2["environment"])

    def test_reboot_unit_files_and_systemctl_calls(self, stub_path, tmp_path):
        units = tmp_path / "units"
        body = "prepare_telegram_bot; install_reboot_unit"
        app, _ = self.run(
            stub_path, tmp_path, body=body, telegram=self.WITH_SECRET, SYSTEMD_DIR=str(units)
        )
        path_unit = (units / "edge-reboot.path").read_text()
        service = (units / "edge-reboot.service").read_text()
        assert f"PathExists={app}/bot-requests/reboot" in path_unit
        assert "Unit=edge-reboot.service" in path_unit and "WantedBy=multi-user.target" in path_unit
        assert "Type=oneshot" in service
        assert f"ExecStartPre=/bin/rm -f {app}/bot-requests/reboot" in service
        assert "$$1 > 600" in service  # systemd needs the doubled dollar
        assert "ExecStart=/usr/bin/systemctl reboot" in service
        log = (stub_path / "log").read_text()
        assert "systemctl daemon-reload" in log
        assert "systemctl enable --now edge-reboot.path" in log

    def test_stale_request_is_removed_before_the_watcher_starts(self, stub_path, tmp_path):
        units = tmp_path / "units"
        app = tmp_path / "app"
        (app / "bot-requests").mkdir(parents=True)
        (app / "bot-requests" / "reboot").touch()
        self.run(
            stub_path,
            tmp_path,
            body="prepare_telegram_bot; install_reboot_unit",
            telegram=self.WITH_SECRET,
            SYSTEMD_DIR=str(units),
        )
        assert not (app / "bot-requests" / "reboot").exists()

    def test_units_are_removed_when_reboot_is_turned_off(self, stub_path, tmp_path):
        units = tmp_path / "units"
        body = "prepare_telegram_bot; install_reboot_unit"
        self.run(stub_path, tmp_path, body=body, telegram=self.WITH_SECRET, SYSTEMD_DIR=str(units))
        assert (units / "edge-reboot.path").exists()
        self.run(stub_path, tmp_path, body=body, telegram=self.GOOD, SYSTEMD_DIR=str(units))
        assert not (units / "edge-reboot.path").exists()
        assert not (units / "edge-reboot.service").exists()
        assert "systemctl disable --now edge-reboot.path" in (stub_path / "log").read_text()

    def test_no_units_written_when_never_enabled(self, stub_path, tmp_path):
        units = tmp_path / "units"
        self.run(
            stub_path,
            tmp_path,
            body="prepare_telegram_bot; install_reboot_unit",
            telegram=self.GOOD,
            SYSTEMD_DIR=str(units),
        )
        assert not units.exists()

    def test_changed_token_is_detected(self, stub_path, tmp_path):
        self.run(stub_path, tmp_path, telegram=self.GOOD)
        new = self.GOOD.replace("1001,2002", "1001")
        body = "prepare_telegram_bot; echo C=$TELEGRAM_CHANGED"
        app, result = self.run(stub_path, tmp_path, body=body, telegram=new)
        assert (
            "C=true" in result.stdout
            and "TELEGRAM_ALLOWED_IDS=1001\n" in (app / ".telegram").read_text()
        )

    def test_start_services_does_not_pull_the_local_image_and_recreates_bot(
        self, stub_path, tmp_path
    ):
        docker = stub_path / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            'echo "docker $*" >> "$STUB_LOG"\n'
            '[[ "$1 $2 $3" == "compose config --services" ]] && printf "frigate\\ntelegram-bot\\n"\n'
            "exit 0\n"
        )
        docker.chmod(0o755)
        (tmp_path / "app" / "frigate").mkdir(parents=True)
        body = "prepare_telegram_bot; create_app_stack\nsleep() { :; }\nstart_services"
        self.run(stub_path, tmp_path, body=body, telegram=self.GOOD)
        log = (stub_path / "log").read_text()
        assert "docker compose pull frigate\n" in log and "pull frigate telegram-bot" not in log
        assert "docker compose up -d --force-recreate --no-deps telegram-bot" in log

    def test_start_services_leaves_bot_alone_when_unchanged(self, stub_path, tmp_path):
        (tmp_path / "app" / "frigate").mkdir(parents=True)
        body = (
            "prepare_telegram_bot; create_app_stack; TELEGRAM_CHANGED=false\n"
            "sleep() { :; }\nstart_services"
        )
        self.run(stub_path, tmp_path, body=body, telegram=self.GOOD)
        assert "force-recreate" not in (stub_path / "log").read_text()

    def test_deploy_main_prepares_bot_before_compose(self):
        text = (REPO / "deploy-edge-server.sh").read_text()
        main_body = text[text.index("main() {") :]
        assert main_body.index("prepare_telegram_bot") < main_body.index("create_app_stack")


# --------------------------------------------------------------------------
# Docker log rotation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script,func,env,services",
    [
        ("deploy-edge-server.sh", "create_app_stack", {}, {"frigate"}),
        (
            "deploy-edge-server.sh",
            "create_app_stack",
            {
                "INSTALL_MQTT": "true",
                "INSTALL_HOMEASSISTANT": "true",
                "INSTALL_TELEGRAM_BOT": "true",
                "MQTT_USER": "mqtt",
                "MQTT_PASS": "pw",
            },
            {"frigate", "mosquitto", "homeassistant", "telegram-bot"},
        ),
    ],
)
def test_every_service_rotates_its_logs(stub_path, tmp_path, script, func, env, services):
    """Docker keeps container logs forever by default; they would fill the disk."""
    full_env = {
        "EDGE_SERVER_DIR": str(tmp_path / "app"),
        "EDGE_STORAGE_DIR": str(tmp_path / "storage"),
        **env,
    }
    result = run_sourced(script, func, stub_path, full_env)
    assert result.returncode == 0, result.stdout + result.stderr
    compose = yaml.safe_load((tmp_path / "app" / "docker-compose.yml").read_text())
    assert set(compose["services"]) == services
    expected = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
    for name, service in compose["services"].items():
        assert service.get("logging") == expected, name


# --------------------------------------------------------------------------
# Unattended-operation hardening: boot without internet, Docker ports,
# narrow bot mounts, image cleanup, disk health timer
# --------------------------------------------------------------------------


class TestUnattendedHardening:
    def run(self, stub_path, tmp_path, body, telegram=None, **env):
        app = tmp_path / "app"
        app.mkdir(exist_ok=True)
        full_env = {
            "EDGE_SERVER_DIR": str(app),
            "EDGE_STORAGE_DIR": str(tmp_path / "storage"),
            "SYSTEMD_DIR": str(tmp_path / "units"),
            "TELEGRAM_CREDENTIALS_FILE": str(tmp_path / ".telegram"),
            **env,
        }
        if telegram is not None:
            (tmp_path / ".telegram").write_text(telegram)
        result = run_sourced("deploy-edge-server.sh", body, stub_path, full_env)
        assert result.returncode == 0, result.stdout + result.stderr
        return app, tmp_path / "units", result

    GOOD = f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_ALLOWED_IDS=1001\n"

    @pytest.fixture(autouse=True)
    def _no_real_dotfile(self):
        if (REPO / ".telegram").exists():
            pytest.skip("a real .telegram exists in the repo checkout")

    # ---- boot without internet ----

    def test_stack_unit_does_not_pull_images_at_boot(self, stub_path, tmp_path):
        script = (
            f"export UNIT_DIR={tmp_path}\n"
            'eval "$(declare -f create_systemd_service '
            "| sed 's|/etc/systemd/system|'$UNIT_DIR'|g')\"\n"
            "create_systemd_service\n"
        )
        self.run(stub_path, tmp_path, script)
        unit = (tmp_path / "edge-server.service").read_text()
        commands = [ln for ln in unit.splitlines() if ln.startswith("Exec")]
        assert commands and not any("pull" in ln for ln in commands)
        assert "ExecStart=/usr/bin/docker compose up -d --remove-orphans" in unit
        assert "edge-docker-firewall.service" in unit  # ports are locked before containers start

    def test_frigate_image_is_pinned(self, stub_path, tmp_path):
        app, _, _ = self.run(stub_path, tmp_path, "create_app_stack")
        image = yaml.safe_load((app / "docker-compose.yml").read_text())["services"]["frigate"][
            "image"
        ]
        assert image.startswith("ghcr.io/blakeblackshear/frigate:")
        assert not image.endswith((":stable", ":latest", ":dev"))

    def test_frigate_image_can_be_overridden(self, stub_path, tmp_path):
        app, _, _ = self.run(
            stub_path, tmp_path, "create_app_stack", FRIGATE_IMAGE="example.com/frigate:9.9.9"
        )
        compose = yaml.safe_load((app / "docker-compose.yml").read_text())
        assert compose["services"]["frigate"]["image"] == "example.com/frigate:9.9.9"

    # ---- the bot sees only what it needs ----

    def test_bot_mounts_are_narrow_and_read_only(self, stub_path, tmp_path):
        app, _, _ = self.run(
            stub_path, tmp_path, "prepare_telegram_bot; create_app_stack", telegram=self.GOOD
        )
        compose = yaml.safe_load((app / "docker-compose.yml").read_text())
        volumes = compose["services"]["telegram-bot"]["volumes"]
        assert all(v.endswith(":ro") for v in volumes)
        targets = {v.split(":")[1] for v in volumes}
        assert "/opt/edge-server" not in targets  # not the whole app directory
        assert targets == {
            "/app",
            "/opt/edge-server/frigate",
            "/opt/edge-server/docker-compose.yml",
            "/status",
            "/media/frigate",
        }
        code = "\n".join(
            ln for ln in (app / "docker-compose.yml").read_text().splitlines() if "#" not in ln
        )
        assert ".cam" not in code and ".telegram:" not in code and "ha-config" not in code

    def test_bot_status_dir_exists_for_the_mount(self, stub_path, tmp_path):
        app, _, _ = self.run(stub_path, tmp_path, "prepare_telegram_bot", telegram=self.GOOD)
        assert stat.S_IMODE((app / "bot-status").stat().st_mode) == 0o755

    # ---- Docker ports ----

    def install_helpers(self, stub_path, tmp_path, **env):
        return self.run(
            stub_path,
            tmp_path,
            "prepare_telegram_bot; install_host_helpers",
            telegram=self.GOOD,
            LOCAL_SUBNET="10.1.0.0/16",
            **env,
        )

    def test_firewall_script_and_unit(self, stub_path, tmp_path):
        app, units, _ = self.install_helpers(stub_path, tmp_path)
        script = app / "docker-firewall.sh"
        assert stat.S_IMODE(script.stat().st_mode) == 0o700
        assert subprocess.run([BASH, "-n", str(script)]).returncode == 0
        text = script.read_text()
        assert 'LOCAL_SUBNET="10.1.0.0/16"' in text and "__" not in text
        unit = (units / "edge-docker-firewall.service").read_text()
        assert f"ExecStart={app}/docker-firewall.sh" in unit
        assert "After=docker.service" in unit and "PartOf=docker.service" in unit
        log = (stub_path / "log").read_text()
        assert "systemctl enable --now edge-docker-firewall.service" in log

    def test_firewall_script_applies_the_expected_rules(self, stub_path, tmp_path):
        app, _, _ = self.install_helpers(stub_path, tmp_path)
        iptables = stub_path / "iptables"
        iptables.write_text(
            "#!/usr/bin/env bash\n"
            'echo "iptables $*" >> "$STUB_LOG"\n'
            '[[ " $* " == *" -C "* ]] && exit 1\n'  # the jump is not installed yet
            "exit 0\n"
        )
        iptables.chmod(0o755)
        (stub_path / "log").write_text("")
        env = {"PATH": f"{stub_path}:{os.environ['PATH']}", "STUB_LOG": str(stub_path / "log")}
        result = run_bash(f"{app}/docker-firewall.sh", env=env)
        assert result.returncode == 0, result.stderr
        rules = [
            line.removeprefix("iptables -w ")
            for line in (stub_path / "log").read_text().splitlines()
            if line.startswith("iptables")
        ]
        a = "-A EDGE-DOCKER-INPUT"
        assert rules.index(f"{a} -s 10.1.0.0/16 -j RETURN") < rules.index(f"{a} -j DROP")
        assert f"{a} -i tailscale0 -j RETURN" in rules
        assert f"{a} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN" in rules
        assert rules[-2] == f"{a} -j DROP" or f"{a} -j DROP" in rules
        assert rules[-1] == "-I DOCKER-USER 1 -j EDGE-DOCKER-INPUT"
        assert "-F EDGE-DOCKER-INPUT" in rules  # rebuilt from scratch, so re-runs do not pile up

    def test_firewall_jump_is_not_added_twice(self, stub_path, tmp_path):
        app, _, _ = self.install_helpers(stub_path, tmp_path)
        (stub_path / "log").write_text("")
        env = {"PATH": f"{stub_path}:{os.environ['PATH']}", "STUB_LOG": str(stub_path / "log")}
        iptables = stub_path / "iptables"
        iptables.write_text('#!/usr/bin/env bash\necho "iptables $*" >> "$STUB_LOG"\nexit 0\n')
        iptables.chmod(0o755)
        run_bash(f"{app}/docker-firewall.sh", env=env)
        assert "-I DOCKER-USER" not in (stub_path / "log").read_text()  # -C succeeded

    # ---- image cleanup ----

    def test_image_cleanup_timer(self, stub_path, tmp_path):
        app, units, _ = self.install_helpers(stub_path, tmp_path)
        script = app / "docker-clean.sh"
        assert subprocess.run([BASH, "-n", str(script)]).returncode == 0
        text = script.read_text()
        assert "for c in frigate telegram-bot; do" in text and "__" not in text
        assert "docker image prune -af" in text
        timer = (units / "edge-docker-clean.timer").read_text()
        assert "OnCalendar=Sun" in timer and "Persistent=true" in timer
        assert "ExecStart=" + str(script) in (units / "edge-docker-clean.service").read_text()
        assert "systemctl enable --now edge-docker-clean.timer" in (stub_path / "log").read_text()

    def run_clean(self, stub_path, app, running):
        docker = stub_path / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            'echo "docker $*" >> "$STUB_LOG"\n'
            f'if [[ "$1" == inspect ]]; then echo {"true" if running else "false"}; fi\n'
            "exit 0\n"
        )
        docker.chmod(0o755)
        (stub_path / "log").write_text("")
        env = {"PATH": f"{stub_path}:{os.environ['PATH']}", "STUB_LOG": str(stub_path / "log")}
        run_bash(f"{app}/docker-clean.sh", env=env)
        return (stub_path / "log").read_text()

    def test_cleanup_prunes_only_while_the_stack_is_running(self, stub_path, tmp_path):
        app, _, _ = self.install_helpers(stub_path, tmp_path)
        assert "docker image prune" in self.run_clean(stub_path, app, running=True)
        assert "docker image prune" not in self.run_clean(stub_path, app, running=False)

    # ---- disk health ----

    def test_disk_health_timer_with_the_bot(self, stub_path, tmp_path):
        app, units, _ = self.install_helpers(stub_path, tmp_path)
        assert (app / "smart-report.py").read_text() == (
            REPO / "telegram-bot/smart-report.py"
        ).read_text()
        assert os.access(app / "smart-report.py", os.X_OK)
        service = (units / "edge-smart.service").read_text()
        assert f"ExecStart={app}/smart-report.py {app}/bot-status/disks.json" in service
        timer = (units / "edge-smart.timer").read_text()
        assert "OnBootSec=" in timer and "OnUnitActiveSec=6h" in timer
        log = (stub_path / "log").read_text()
        assert "systemctl enable --now edge-smart.timer" in log
        assert "systemctl start --no-block edge-smart.service" in log

    def test_no_disk_health_timer_without_the_bot(self, stub_path, tmp_path):
        app, units, _ = self.run(stub_path, tmp_path, "install_host_helpers")
        assert not (units / "edge-smart.timer").exists()
        assert not (app / "smart-report.py").exists()
        assert (units / "edge-docker-firewall.service").exists()  # the rest is always installed

    def test_disk_health_units_removed_when_bot_is_turned_off(self, stub_path, tmp_path):
        app, units, _ = self.install_helpers(stub_path, tmp_path)
        assert (units / "edge-smart.timer").exists()
        self.run(
            stub_path,
            tmp_path,
            "prepare_telegram_bot; install_host_helpers",
            INSTALL_TELEGRAM_BOT="false",
        )
        assert not (units / "edge-smart.timer").exists()
        assert not (units / "edge-smart.service").exists()
        assert not (app / "smart-report.py").exists()
        assert "systemctl disable --now edge-smart.timer" in (stub_path / "log").read_text()

    def test_smartmontools_is_installed(self):
        assert "smartmontools" in (REPO / "deploy-edge-server.sh").read_text()
