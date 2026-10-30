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
        if not Path("/dev/dri/renderD128").exists():
            # Without an Intel GPU the device mapping would break `compose up`
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


class TestDeploySecurity:
    def make(self, stub_path, tmp_path, **extra):
        app = tmp_path / "app"
        env = {
            "EDGE_SERVER_DIR": str(app),
            "EDGE_STORAGE_DIR": str(tmp_path / "storage"),
            "REOLINK_USER": "admin",
            "REOLINK_PASS": "campass",
        }
        env.update(extra)
        result = run_sourced("deploy-security.sh", "create_configs", stub_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        return app

    def test_honours_edge_server_dir(self, stub_path, tmp_path):
        app = self.make(stub_path, tmp_path)
        assert (app / "docker-compose.yml").exists()
        env_file = (app / ".env").read_text()
        assert f"STORAGE_PATH={tmp_path / 'storage'}/frigate" in env_file
        assert stat.S_IMODE((app / ".env").stat().st_mode) == 0o600

    def test_mqtt_auth_when_credentials_given(self, stub_path, tmp_path):
        app = self.make(stub_path, tmp_path, MQTT_USER="mq", MQTT_PASS="pw")
        conf = (app / "mosquitto/config/mosquitto.conf").read_text()
        assert "allow_anonymous false" in conf
        assert "password_file /mosquitto/config/passwd" in conf
        frigate = yaml.safe_load((app / "frigate/config.yml").read_text())
        assert frigate["mqtt"]["user"] == "{FRIGATE_MQTT_USER}"
        assert frigate["mqtt"]["password"] == "{FRIGATE_MQTT_PASSWORD}"
        assert "FRIGATE_MQTT_PASSWORD=pw" in (app / ".env").read_text()

    def test_anonymous_without_credentials(self, stub_path, tmp_path):
        app = self.make(stub_path, tmp_path)
        conf = (app / "mosquitto/config/mosquitto.conf").read_text()
        assert "allow_anonymous true" in conf
        frigate = yaml.safe_load((app / "frigate/config.yml").read_text())
        assert "user" not in frigate["mqtt"]

    def test_generated_files_are_valid_yaml(self, stub_path, tmp_path):
        app = self.make(stub_path, tmp_path, MQTT_USER="mq", MQTT_PASS="pw")
        compose = yaml.safe_load((app / "docker-compose.yml").read_text())
        assert set(compose["services"]) == {"mosquitto", "frigate", "homeassistant"}
        hc = compose["services"]["mosquitto"]["healthcheck"]["test"]
        assert hc[0] == "CMD-SHELL"
        frigate = yaml.safe_load((app / "frigate/config.yml").read_text())
        assert len(frigate["cameras"]) == 4
        assert stat.S_IMODE((app / "frigate/config.yml").stat().st_mode) == 0o600
        ha = yaml.safe_load((app / "ha-config/configuration.yaml").read_text())
        assert "mqtt" not in ha

    @pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
    def test_compose_config_validates(self, stub_path, tmp_path):
        app = self.make(stub_path, tmp_path, MQTT_USER="mq", MQTT_PASS="pw")
        real = subprocess.run(
            ["docker", "compose", "-f", str(app / "docker-compose.yml"), "config", "-q"],
            capture_output=True,
            text=True,
            cwd=app,
        )
        if "is not a docker command" in real.stderr:
            pytest.skip("docker compose plugin not installed")
        assert real.returncode == 0, real.stderr


# --------------------------------------------------------------------------
# Re-deploy (update) must not wipe a customised Frigate config
# --------------------------------------------------------------------------

CUSTOM_FRIGATE = "# my cameras\ncameras:\n  barn_cam:\n    enabled: true\n"


@pytest.mark.parametrize(
    "script,func,extra_env",
    [
        ("deploy-edge-server.sh", "create_app_stack", {"MQTT_USER": "u", "MQTT_PASS": "p"}),
        (
            "deploy-security.sh",
            "create_configs",
            {"REOLINK_USER": "admin", "REOLINK_PASS": "campass"},
        ),
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
            ("deploy-security.sh", "create_configs", {"REOLINK_USER": "a", "REOLINK_PASS": "b"}),
        ],
    )
    def test_generated_config_has_no_empty_mask(self, stub_path, tmp_path, script, func, env):
        full_env = {"EDGE_SERVER_DIR": str(tmp_path / "app"), **env}
        result = run_sourced(script, func, stub_path, full_env)
        assert result.returncode == 0, result.stdout + result.stderr
        cfg = yaml.safe_load((tmp_path / "app" / "frigate" / "config.yml").read_text())
        assert not _has_empty_mask(cfg)

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
