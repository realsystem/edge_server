"""ha-addon/addon-setup.py: the add-on's options become the bot's environment, services and fleet file."""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("addon_setup", REPO / "ha-addon/addon-setup.py")
assert spec and spec.loader
ads = importlib.util.module_from_spec(spec)
sys.modules["addon_setup"] = ads
spec.loader.exec_module(ads)

TOKEN = "123456789:" + "A" * 35
GOOD = {
    "telegram_bot_token": TOKEN,
    "allowed_ids": "1001, 1002",
    "owner_totp_secret": "gezd gnbv-gy3t qojq gezd gnbv gy3t qojq",
    "node": "",
    "restart_addons": ["core_mosquitto"],
    "peer_grants": "",
}


def run(tmp_path, **over):
    opts = {**GOOD, **over}
    options = tmp_path / "options.json"
    options.write_text(json.dumps(opts))
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return ads.setup(options, data), data


def env_of(data):
    out = {}
    for line in (data / "app/env").read_text().splitlines():
        k, _, v = line.removeprefix("export ").partition("=")
        out[k] = shlex.split(v)[0] if v else ""
    return out


def test_options_become_the_bots_environment(tmp_path):
    log, data = run(tmp_path)
    env = env_of(data)
    assert env["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert env["TELEGRAM_ALLOWED_IDS"] == "1001,1002"
    assert env["OWNER_TOTP_SECRET"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert env["FRIGATE_URL"] == "" and env["SERVICES"] == "core core_mosquitto"
    assert env["REQUESTS_DIR"] == str(data / "app/bot-requests")
    assert (data / "app/env").stat().st_mode & 0o777 == 0o600
    assert (data / "app/bot-requests").stat().st_mode & 0o777 == 0o700
    assert any("runs alone" in line for line in log)


def test_the_restartable_names_are_written_as_a_compose_file_the_bot_reads(tmp_path):
    _, data = run(tmp_path)
    text = (data / "app/docker-compose.yml").read_text()
    assert "  core:\n" in text and "  core_mosquitto:\n" in text


@pytest.mark.parametrize(
    "over, message",
    [
        ({"telegram_bot_token": "nope"}, "telegram_bot_token"),
        ({"allowed_ids": ""}, "allowed_ids"),
        ({"allowed_ids": "alice"}, "allowed_ids"),
        ({"node": "Pi 1"}, "node must be"),
        ({"restart_addons": ["../x"]}, "restart_addons"),
    ],
)
def test_bad_options_stop_the_start_with_a_reason(tmp_path, over, message):
    with pytest.raises(ads.SetupError, match=message):
        run(tmp_path, **over)


def test_an_empty_secret_is_allowed_and_said_in_the_log(tmp_path):
    log, _ = run(tmp_path, owner_totp_secret="")
    assert any("stay off" in line for line in log)


def test_a_secret_with_odd_characters_survives_the_env_file(tmp_path):
    _, data = run(tmp_path, peer_grants="edge-1:restart=core,reboot")
    assert env_of(data)["PEER_GRANTS"] == "edge-1:restart=core,reboot"


def test_with_a_fleet_file_the_key_is_made_once_and_the_fleet_described(tmp_path):
    pytest.importorskip("cryptography")
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text("x: 1\n")
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake(cmd, **kw):
        calls.append(cmd)
        return Done()

    options = tmp_path / "o.json"
    options.write_text(json.dumps({**GOOD, "node": "pi-1", "fleet_file": str(fleet)}))
    data = tmp_path / "d"
    data.mkdir()
    log = ads.setup(options, data, run_fleet=fake)
    key1 = (data / "node.key").read_bytes()
    assert (data / "node.key").stat().st_mode & 0o777 == 0o600
    assert any("public key" in line for line in log) and "bot-config" in calls[0]
    assert calls[0][calls[0].index("--node") + 1] == "pi-1"
    ads.setup(options, data, run_fleet=fake)
    assert (data / "node.key").read_bytes() == key1  # not replaced on the next start


def test_a_refused_fleet_file_stops_the_start(tmp_path):
    pytest.importorskip("cryptography")
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text("x: 1\n")

    class Done:
        returncode, stdout, stderr = 1, "line 3: unknown role", ""

    options = tmp_path / "o.json"
    options.write_text(json.dumps({**GOOD, "node": "pi-1", "fleet_file": str(fleet)}))
    data = tmp_path / "d"
    data.mkdir()
    with pytest.raises(ads.SetupError, match="unknown role"):
        ads.setup(options, data, run_fleet=lambda *a, **k: Done())


def test_main_reports_errors_on_stderr_and_exits_1(tmp_path, capsys):
    (tmp_path / "o.json").write_text("{}")
    assert ads.main([str(tmp_path / "o.json"), str(tmp_path)]) == 1
    assert "telegram_bot_token" in capsys.readouterr().err


def test_the_bot_reads_the_written_services(tmp_path):
    sys.path.insert(0, str(REPO / "tests"))
    from test_telegram_bot import TOKEN as BOT_TOKEN
    from test_telegram_bot import bot

    _, data = run(tmp_path)
    cfg = bot.Config(token=BOT_TOKEN, app_dir=data / "app")
    assert bot.compose_services(cfg) == ["core", "core_mosquitto"]
