"""tools/fleet.py: the fleet file loader, validator, settings mapping and one-node writer."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("fleet", REPO / "tools/fleet.py")
assert spec and spec.loader
fleet = importlib.util.module_from_spec(spec)
sys.modules["fleet"] = fleet
spec.loader.exec_module(fleet)

EXAMPLES = sorted((REPO / "examples").glob("fleet.*.yaml"))
MINIMAL = (REPO / "examples/fleet.minimal.yaml").read_text()
TWO_SITE = (REPO / "examples/fleet.two-site.yaml").read_text()


def errors_of(text: str) -> list[tuple[int, str]]:
    with pytest.raises(fleet.FleetError) as caught:
        fleet.parse(text)
    return caught.value.errors


def messages(text: str) -> str:
    return "\n".join(m for _, m in errors_of(text))


def mutated(base: str, change) -> str:
    """The example with ``change(data)`` applied, dumped back as YAML."""
    data = yaml.safe_load(base)
    change(data)
    return yaml.safe_dump(data, sort_keys=False)


class TestExamples:
    @pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
    def test_validate(self, path):
        data, _ = fleet.parse_file(str(path))
        assert data["schema_version"] == 1

    @pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
    def test_match_the_json_schema(self, path):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads((REPO / "examples/fleet.schema.json").read_text())
        jsonschema.validate(yaml.safe_load(path.read_text()), schema)

    def test_there_are_examples(self):
        assert {p.name for p in EXAMPLES} >= {"fleet.minimal.yaml", "fleet.two-site.yaml"}

    def test_the_schema_is_valid_and_refuses_what_the_checker_refuses(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads((REPO / "examples/fleet.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        bad = [
            lambda d: d["nodes"]["node-1"].update(colour="red"),  # unknown setting
            lambda d: d["nodes"]["node-1"].update(packaging="vm"),
            lambda d: d["nodes"]["node-1"].update(roles=["wizard"]),
            lambda d: d["nodes"]["node-1"].update(bot_id="abc"),
            lambda d: d.update(schema_version=2),
            lambda d: d["fleet"]["telegram"].update(operators=["x"]),
            lambda d: d["sites"]["site-1"].update(lan_subnet="lan"),
        ]
        for change in bad:
            doc = yaml.safe_load(MINIMAL)
            change(doc)
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(doc, schema)
            with pytest.raises(fleet.FleetError):
                fleet.parse(yaml.safe_dump(doc))


class TestValidation:
    def test_duplicate_keys_are_an_error_with_their_line(self):
        text = MINIMAL.replace("  name: my-fleet\n", "  name: a\n  name: b\n")
        found = errors_of(text)
        assert any("duplicate key 'name'" in m for _, m in found)
        line = next(n for n, m in found if "duplicate" in m)
        assert text.splitlines()[line - 1].strip() == "name: b"

    def test_unknown_setting_names_its_line(self):
        text = MINIMAL.replace(
            "    tailscale: node-1\n", "    tailscale: node-1\n    colour: red\n"
        )
        found = errors_of(text)
        assert len(found) == 1
        line, message = found[0]
        assert "unknown setting 'colour'" in message
        assert text.splitlines()[line - 1].strip() == "colour: red"

    def test_every_problem_is_reported_not_just_the_first(self):
        def change(d):
            d["nodes"]["node-1"]["roles"] = ["wizard"]
            d["nodes"]["node-1"]["site"] = "nowhere"
            d["sites"]["site-1"]["timezone"] = "Mars/Base"

        text = messages(mutated(MINIMAL, change))
        assert "unknown role 'wizard'" in text
        assert "site 'nowhere' is not defined" in text
        assert "unknown time zone 'Mars/Base'" in text

    def test_schema_version_is_required_and_a_newer_one_is_refused(self):
        assert "schema_version is missing" in messages(MINIMAL.replace("schema_version: 1\n", ""))
        newer = messages(MINIMAL.replace("schema_version: 1", "schema_version: 2"))
        assert "newer than this release understands (1)" in newer

    def test_not_yaml_and_empty(self):
        assert "not valid YAML" in messages("a: [unclosed\n")
        assert "the file is empty" in messages("")
        assert "must be a mapping" in messages("- just\n- a list\n")

    def test_unreadable_file(self):
        with pytest.raises(fleet.FleetError, match="2 error|1 error"):
            fleet.parse_file("/nonexistent/fleet.yaml")

    @pytest.mark.parametrize("name", ["Node_1", "-x", "x-", "has space", "ünï"])
    def test_names_are_safe_everywhere(self, name):
        def change(d):
            d["nodes"][name] = d["nodes"].pop("node-1")
            d["fleet"]["alert_order"] = [name]

        assert "use lowercase letters, digits and hyphens" in messages(mutated(MINIMAL, change))

    def test_alert_order_must_cover_every_node_once(self):
        def missing(d):
            d["fleet"]["alert_order"] = ["home-server"]

        assert "does not list node 'remote-nvr'" in messages(mutated(TWO_SITE, missing))

        def twice(d):
            d["fleet"]["alert_order"] = d["fleet"]["alert_order"] + ["home-server"]

        assert "listed twice" in messages(mutated(TWO_SITE, twice))

        def stranger(d):
            d["fleet"]["alert_order"] = d["fleet"]["alert_order"] + ["ghost"]

        assert "'ghost' is not a node" in messages(mutated(TWO_SITE, stranger))

    def test_archive_target_must_be_an_archive_node(self):
        def change(d):
            d["nodes"]["remote-nvr"]["nvr"]["archive_target"] = "remote-ha"

        assert "not a node with the archive role" in messages(mutated(TWO_SITE, change))

    def test_archive_role_needs_arrays(self):
        def change(d):
            del d["nodes"]["home-server"]["archive"]["arrays"]

        assert "archive role needs archive.arrays" in messages(mutated(TWO_SITE, change))

    def test_addon_nodes_only_run_ha(self):
        def change(d):
            d["nodes"]["remote-ha"]["roles"] = ["ha", "nvr"]

        assert "an ha-addon node can only have the role ha" in messages(mutated(TWO_SITE, change))

    def test_bot_ids_are_unique(self):
        def change(d):
            d["nodes"]["home-ha"]["bot_id"] = d["nodes"]["remote-ha"]["bot_id"]

        assert "also used by node 'remote-ha'" in messages(mutated(TWO_SITE, change))

    def test_power_levels_must_fall(self):
        def change(d):
            d["sites"]["remote"]["power"] = {"conserve": 20, "shed": 25, "survive": 15, "dark": 10}

        assert "must fall" in messages(mutated(TWO_SITE, change))

    def test_relay_owner_is_a_node_at_that_site(self):
        def change(d):
            d["sites"]["remote"]["relays"][0]["owner"] = "home-server"

        assert "is not a node at site remote" in messages(mutated(TWO_SITE, change))

    def test_true_false_where_numbers_belong(self):
        def change(d):
            d["nodes"]["node-1"]["bot_id"] = True

        assert "bot_id must be" in messages(mutated(MINIMAL, change))

    def test_bluetooth_address_and_lan_address(self):
        def change(d):
            d["nodes"]["remote-nvr"]["power"]["victron"]["address"] = "nope"
            d["nodes"]["remote-nvr"]["lan"] = "192.168.10.999"

        text = messages(mutated(TWO_SITE, change))
        assert "Bluetooth address" in text and "is not an IPv4 address" in text

    def test_warnings_do_not_fail_the_file(self):
        def change(d):
            d["fleet"]["telegram"]["operators"] = []

        _, warnings = fleet.parse(mutated(MINIMAL, change))
        assert any("nobody can use the bots" in m for _, m in warnings)

    def test_settings_for_roles_a_node_does_not_run_are_ignored_not_errors(self):
        def change(d):
            d["nodes"]["node-1"]["archive"] = {"retention_days": {"events": 5}}

        fleet.parse(mutated(MINIMAL, change))


class TestSettings:
    @pytest.fixture
    def two(self):
        return fleet.parse(TWO_SITE)[0]

    def test_a_host_node_maps_onto_todays_variables(self, two):
        s = fleet.settings(two, "remote-nvr")
        assert s["INSTALL_HOMEASSISTANT"] == "false" and s["INSTALL_MQTT"] == "false"
        assert s["VICTRON_INSTALL"] == "true" and s["VICTRON_ADDRESS"] == "AA:BB:CC:DD:EE:01"
        assert s["RENOGY_INSTALL"] == "true" and s["RENOGY_ADDRESS"] == "AA:BB:CC:DD:EE:02"
        assert s["TIMEZONE"] == "UTC" and s["EDGE_STORAGE_DIR"] == "/mnt/storage"
        assert s["EDGE_NODE"] == "remote-nvr" and s["EDGE_SITE"] == "remote"
        assert s["EDGE_OPERATORS"] == "100000001"
        assert s["FLEET_ROLES"] == "base,nvr,power,agent"

    def test_without_the_power_role_the_ble_addons_are_off_without_asking(self, two):
        s = fleet.settings(two, "home-server")
        assert s["VICTRON_INSTALL"] == "false" and s["RENOGY_INSTALL"] == "false"
        assert "VICTRON_ADDRESS" not in s

    def test_role_override_replaces_the_nodes_roles_for_the_run(self, two):
        s = fleet.settings(two, "remote-nvr", ["nvr", "ha"])
        assert s["INSTALL_HOMEASSISTANT"] == "true"
        assert s["VICTRON_INSTALL"] == "false"
        assert s["FLEET_ROLES"] == "base,nvr,ha"

    def test_ha_check_follows_the_ha_node_at_the_same_site(self, two):
        assert fleet.settings(two, "remote-nvr")["HA_HOST"] == "192.168.10.2"
        # the home site's ha node has no lan address: no check rather than a wrong one
        assert fleet.settings(two, "home-server")["HA_HOST"] == ""

    def test_ha_port_comes_from_the_ha_node(self):
        def change(d):
            d["nodes"]["remote-ha"]["ha"] = {"port": 8124}

        s = fleet.settings(fleet.parse(mutated(TWO_SITE, change))[0], "remote-nvr")
        assert (s["HA_HOST"], s["HA_PORT"]) == ("192.168.10.2", "8124")

    def test_no_ha_node_means_no_check(self):
        s = fleet.settings(fleet.parse(MINIMAL)[0], "node-1")
        assert s["HA_HOST"] == "" and s["HA_PORT"] == "8123"

    def test_repo_and_subnet_only_when_given(self):
        s = fleet.settings(fleet.parse(MINIMAL)[0], "node-1")
        assert "REDEPLOY_REPO" not in s and "LOCAL_SUBNET" not in s

        def change(d):
            d["fleet"]["update"] = {"repo": "https://example.org/me/edge_server.git"}
            d["sites"]["site-1"]["lan_subnet"] = "10.1.2.0/24"

        s = fleet.settings(fleet.parse(mutated(MINIMAL, change))[0], "node-1")
        assert s["REDEPLOY_REPO"] == "https://example.org/me/edge_server.git"
        assert s["LOCAL_SUBNET"] == "10.1.2.0/24"

    def test_assignments_yield_to_variables_already_set_and_are_quoted(self):
        out = fleet.shell_lines({"A": "x y", "B": "it's"})
        assert out.splitlines()[0] == "[[ -v A ]] || A='x y'"
        assert out.splitlines()[1] == "[[ -v B ]] || B='it'\"'\"'s'"

    def test_cannot_deploy_what_this_release_cannot_install(self, two):
        assert "replace the <placeholder>" in fleet.deploy_blockers(two, "remote-nvr", None)[0]
        real = fleet.parse(TWO_SITE.replace("<filesystem uuid>", "1234-ABCD"))[0]
        assert "add-on" in fleet.deploy_blockers(real, "remote-ha", None)[0]
        assert "no nvr role" in fleet.deploy_blockers(real, "remote-nvr", ["ha"])[0]

    def test_a_real_value_deploys(self):
        text = MINIMAL.replace("<filesystem uuid>", "1234-ABCD")
        assert fleet.deploy_blockers(fleet.parse(text)[0], "node-1", None) == []

    def test_roles_argument(self):
        assert fleet.parse_roles("nvr, ha") == ["nvr", "ha"]
        for bad in ("", "nvr,wizard"):
            with pytest.raises(fleet.FleetError):
                fleet.parse_roles(bad)


class TestWriteOneNode:
    ENV = {
        "INSTALL_HOMEASSISTANT": "false",
        "INSTALL_MQTT": "true",
        "VICTRON_INSTALL": "true",
        "VICTRON_ADDRESS": "AA:BB:CC:DD:EE:01",
        "RENOGY_INSTALL": "false",
        "TIMEZONE": "Europe/Berlin",
        "LOCAL_SUBNET": "10.1.2.0/24",
        "EXTERNAL_DRIVE_UUID": "1234-ABCD",
        "EDGE_STORAGE_DIR": "/mnt/rec",
        "REDEPLOY_REPO": "https://example.org/me/edge_server.git",
    }

    @pytest.fixture
    def telegram(self, tmp_path):
        path = tmp_path / ".telegram"
        path.write_text(
            "TELEGRAM_BOT_TOKEN=123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
            "TELEGRAM_ALLOWED_IDS=111, 222\nREBOOT_TOTP_SECRET=ABCDEFGHIJKLMNOP\n"
        )
        return str(path)

    def test_the_file_passes_the_checker_and_round_trips_the_settings(self, telegram):
        text = fleet.write_one_node("edge-1", self.ENV, telegram, "10.0.0.2")
        data, _ = fleet.parse(text)
        s = fleet.settings(data, "edge-1")
        assert s["INSTALL_HOMEASSISTANT"] == "false" and s["INSTALL_MQTT"] == "true"
        assert (s["VICTRON_INSTALL"], s["VICTRON_ADDRESS"]) == ("true", "AA:BB:CC:DD:EE:01")
        assert s["RENOGY_INSTALL"] == "false"
        assert s["TIMEZONE"] == "Europe/Berlin" and s["LOCAL_SUBNET"] == "10.1.2.0/24"
        assert s["EXTERNAL_DRIVE_UUID"] == "1234-ABCD" and s["EDGE_STORAGE_DIR"] == "/mnt/rec"
        assert s["REDEPLOY_REPO"] == "https://example.org/me/edge_server.git"
        assert s["EDGE_OPERATORS"] == "111,222" and s["EDGE_BOT_ID"] == "123456789"
        assert s["HA_HOST"] == "10.0.0.2"  # the check this install ran keeps running

    def test_a_node_that_runs_home_assistant_itself_gets_no_extra_node(self):
        text = fleet.write_one_node("edge-1", {"INSTALL_HOMEASSISTANT": "true"}, None, "10.0.0.2")
        data, _ = fleet.parse(text)
        assert list(data["nodes"]) == ["edge-1"] and "ha" in data["nodes"]["edge-1"]["roles"]
        assert fleet.settings(data, "edge-1")["INSTALL_HOMEASSISTANT"] == "true"

    def test_no_secret_is_copied(self, telegram):
        text = fleet.write_one_node("edge-1", self.ENV, telegram)
        assert "AAAAAAAA" not in text and "ABCDEFGHIJKLMNOP" not in text

    def test_a_bare_install_gets_a_minimal_valid_file(self):
        data, _ = fleet.parse(fleet.write_one_node("edge-1", {}, None))
        s = fleet.settings(data, "edge-1")
        assert s["FLEET_ROLES"] == "base,nvr" and s["TIMEZONE"] == "UTC"
        assert s["HA_HOST"] == ""  # no ha node known: no check, said plainly

    def test_the_upstream_repo_is_not_written_down(self):
        env = {"REDEPLOY_REPO": fleet.UPSTREAM_REPO}
        assert "repo" not in fleet.write_one_node("edge-1", env, None)

    def test_a_host_name_for_home_assistant_needs_a_hand_edit(self):
        with pytest.raises(fleet.FleetError, match="2 error|1 error"):
            fleet.write_one_node("edge-1", {}, None, "ha.local")

    def test_bad_node_name(self):
        with pytest.raises(fleet.FleetError):
            fleet.write_one_node("Edge 1", {}, None)


class TestCommandLine:
    def run(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(REPO / "tools/fleet.py"), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_check_ok_and_error_exit_codes(self, tmp_path):
        good = REPO / "examples/fleet.minimal.yaml"
        assert self.run("check", str(good)).returncode == 0
        bad = tmp_path / "bad.yaml"
        bad.write_text(MINIMAL.replace("packaging: host", "packaging: vm"))
        result = self.run("check", str(bad))
        assert result.returncode == 1
        assert f"{bad}:" in result.stderr and "packaging must be one of" in result.stderr

    def test_check_with_a_node_prints_the_plan(self):
        out = self.run("check", str(REPO / "examples/fleet.two-site.yaml"), "--node", "remote-nvr")
        assert out.returncode == 0
        assert "node remote-nvr at site remote, host, roles: base, nvr, power, agent" in out.stdout
        assert "not implemented in this release (ignored): agent" in out.stdout

    def test_unknown_node(self):
        out = self.run("check", str(REPO / "examples/fleet.minimal.yaml"), "--node", "nope")
        assert out.returncode == 1 and "'nope' is not in the file" in out.stderr

    def test_env_refuses_to_print_anything_for_an_undeployable_node(self, tmp_path):
        out = self.run("env", str(REPO / "examples/fleet.two-site.yaml"), "--node", "remote-ha")
        assert out.returncode == 3 and out.stdout == ""

    def test_env_prints_assignments(self, tmp_path):
        good = tmp_path / "f.yaml"
        good.write_text(MINIMAL.replace("<filesystem uuid>", "1234-ABCD"))
        out = self.run("env", str(good), "--node", "node-1")
        assert out.returncode == 0
        assert "[[ -v EXTERNAL_DRIVE_UUID ]] || EXTERNAL_DRIVE_UUID=1234-ABCD" in out.stdout

    def test_write_refuses_to_overwrite(self, tmp_path):
        target = tmp_path / "fleet.yaml"
        target.write_text("keep me")
        out = self.run("write", "--node", "edge-1", "--out", str(target))
        assert out.returncode == 1 and target.read_text() == "keep me"
        assert "already exists" in out.stderr

    def test_write_then_check(self, tmp_path):
        target = tmp_path / "fleet.yaml"
        assert self.run("write", "--node", "edge-1", "--out", str(target)).returncode == 0
        assert self.run("check", str(target), "--node", "edge-1").returncode == 0


class TestControlPlaneSettings:
    KEY = "A" * 43 + "="  # 32 bytes of base64

    def test_one_node_has_no_control_plane(self):
        s = fleet.settings(fleet.parse(MINIMAL)[0], "node-1")
        assert s["CONTROL_PLANE"] == "false" and s["CONTROL_PORT"] == "7443"

    def test_two_nodes_do(self):
        s = fleet.settings(fleet.parse(TWO_SITE)[0], "remote-nvr")
        assert s["CONTROL_PLANE"] == "true"

    def test_the_port_is_a_fleet_setting(self):
        def change(d):
            d["fleet"]["control_port"] = 9000

        assert (
            fleet.settings(fleet.parse(mutated(TWO_SITE, change))[0], "remote-nvr")["CONTROL_PORT"]
            == "9000"
        )

        def low(d):
            d["fleet"]["control_port"] = 80

        assert "control_port must be a number from 1024" in messages(mutated(TWO_SITE, low))

    def test_public_key_must_be_32_bytes_of_base64(self):
        def good(d):
            d["nodes"]["remote-nvr"]["public_key"] = self.KEY

        fleet.parse(mutated(TWO_SITE, good))
        for bad in ("short", "!!!!", "QUFB", True):

            def change(d, bad=bad):
                d["nodes"]["remote-nvr"]["public_key"] = bad

            assert "public_key" in messages(mutated(TWO_SITE, change))

    def test_a_missing_key_is_a_warning_only_in_a_fleet_of_several(self):
        assert not any("public_key" in m for _, m in fleet.parse(MINIMAL)[1])
        warned = [m for _, m in fleet.parse(TWO_SITE)[1] if "public_key" in m]
        assert len(warned) == 4

        def keys(d):
            for node in d["nodes"].values():
                node["public_key"] = self.KEY

        assert not any("public_key" in m for _, m in fleet.parse(mutated(TWO_SITE, keys))[1])


class TestBotConfig:
    def resolver(self, table):
        return lambda name=None: table.get(name, "")

    def config(self, resolve=None, node="remote-nvr", version="v1.2"):
        data = fleet.parse(TWO_SITE)[0]
        raw = TWO_SITE.encode()
        return fleet.bot_config(data, node, raw, version, resolve or self.resolver({})), raw

    def test_describes_this_node_and_its_peers(self):
        (cfg, own), raw = self.config(
            self.resolver({None: "100.64.0.9", "remote-ha": "100.64.0.2"})
        )
        assert cfg["node"] == "remote-nvr" and cfg["site"] == "remote" and cfg["port"] == 7443
        assert cfg["version"] == "v1.2" and cfg["fleet_hash"] == fleet.file_hash(raw)
        assert cfg["roles"] == ["base", "nvr", "power", "agent"]
        assert [p["name"] for p in cfg["peers"]] == ["remote-ha", "home-server", "home-ha"]
        assert own == ["192.168.10.10", "100.64.0.9"]

    def test_addresses_per_path(self):
        (cfg, _), _ = self.config(self.resolver({"remote-ha": "100.64.0.2"}))
        peers = {p["name"]: p for p in cfg["peers"]}
        # same site: Tailscale (resolved) and LAN
        assert peers["remote-ha"]["addrs"] == {"tailscale": "100.64.0.2", "lan": "192.168.10.2"}
        # another site: Tailscale only, and the name when it cannot be resolved
        assert peers["home-server"]["addrs"] == {"tailscale": "home-server"}

    def test_public_keys_are_passed_on_and_nothing_secret_is(self):
        def change(d):
            d["nodes"]["remote-ha"]["public_key"] = "A" * 43 + "="

        data = fleet.parse(mutated(TWO_SITE, change))[0]
        cfg, _ = fleet.bot_config(data, "remote-nvr", b"x", "", self.resolver({}))
        assert {p["name"]: p["public_key"] for p in cfg["peers"]}["remote-ha"] == "A" * 43 + "="
        text = json.dumps(cfg)
        assert "AA:BB:CC" not in text and "victron" not in text  # device addresses stay on the host

    def test_a_node_with_no_address_at_all_publishes_nowhere(self):
        data = fleet.parse(TWO_SITE)[0]
        _, own = fleet.bot_config(data, "home-server", b"x", "", self.resolver({}))
        assert own == []

    def test_cli_writes_the_file_and_prints_the_addresses(self, tmp_path):
        out = tmp_path / "fleet.json"
        result = subprocess.run(
            [sys.executable, str(REPO / "tools/fleet.py"), "bot-config",
             str(REPO / "examples/fleet.two-site.yaml"), "--node", "remote-nvr",
             "--out", str(out), "--version", "v1.2"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("CONTROL_PUBLISH=")
        assert json.loads(out.read_text())["node"] == "remote-nvr"
        assert not (tmp_path / "fleet.json.tmp").exists()

    def test_cli_refuses_an_invalid_file(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("schema_version: 9\n")
        result = subprocess.run(
            [sys.executable, str(REPO / "tools/fleet.py"), "bot-config", str(bad),
             "--node", "x", "--out", str(tmp_path / "o.json")],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert result.returncode == 1 and not (tmp_path / "o.json").exists()
