# ruff: noqa: F811, F401, E402 - pytest fixtures are imported from test_telegram_bot and used as arguments
"""/farm (Autonomy M10): what the Field Station cell last saw. Read only; stdlib only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))
import farm
from test_telegram_bot import NOW, TOKEN, bot, frigate, host, make_bot, send, telegram


def doc(t=NOW, connected=True, entities=None):
    return {
        "cell": "field-station",
        "updated_at": t,
        "connected": connected,
        "uptime_s": 3600,
        "entities": (
            entities
            if entities is not None
            else {
                "sensor.ws90_temperature": {
                    "state": "35.0",
                    "unit": "°C",
                    "name": "WS90 temperature",
                    "observed_at": t - 5,
                },
                "sensor.wh51_ch1_soil_moisture": {
                    "state": "41",
                    "unit": "%",
                    "name": "Soil moisture ch1",
                    "observed_at": t - 130,
                },
            }
        ),
    }


def write(tmp_path, d):
    p = tmp_path / "status.json"
    p.write_text(json.dumps(d))
    return p


class TestText:
    def test_shows_readings_with_units_and_age(self, tmp_path):
        out = farm.farm_text(write(tmp_path, doc()), lambda: NOW)
        assert "connected to Home Assistant, 2 entities" in out
        assert "WS90 temperature: 35 °C  (5 s ago)" in out
        assert "Soil moisture ch1: 41 %  (2 min ago)" in out

    def test_missing_or_broken_file_says_so(self, tmp_path):
        assert "has not reported" in farm.farm_text(tmp_path / "nope.json", lambda: NOW)
        bad = tmp_path / "status.json"
        bad.write_text("{not json")
        assert "has not reported" in farm.farm_text(bad, lambda: NOW)
        bad.write_text('{"entities": 5}')
        assert "has not reported" in farm.farm_text(bad, lambda: NOW)

    def test_old_file_is_not_presented_as_current(self, tmp_path):
        out = farm.farm_text(write(tmp_path, doc(t=NOW - 3600)), lambda: NOW)
        assert "NOT REPORTING (last word 60 min ago)" in out
        assert "connected to Home Assistant" not in out

    def test_home_assistant_away_keeps_last_values(self, tmp_path):
        out = farm.farm_text(write(tmp_path, doc(connected=False)), lambda: NOW)
        assert "Home Assistant is unreachable" in out and "WS90 temperature" in out

    def test_no_entities_yet(self, tmp_path):
        assert "No allowlisted entity" in farm.farm_text(
            write(tmp_path, doc(entities={})), lambda: NOW
        )


class TestBot:
    def test_off_without_a_cell(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        send(b, "/farm")
        assert "/farm is off" in telegram.texts()[-1]
        assert "farm" not in [c["command"] for c in b.commands()]
        assert "/farm" not in b.help_text()

    def test_answers_from_the_status_file(self, telegram, frigate, host, tmp_path):
        b = make_bot(telegram, frigate, host, farm_status=write(tmp_path, doc()))
        b.clock = lambda: NOW
        send(b, "/farm")
        assert "WS90 temperature: 35 °C" in telegram.texts()[-1]
        assert "farm" in [c["command"] for c in b.commands()]
        assert "/farm" in b.help_text()

    def test_unauthorized_user_gets_nothing(self, telegram, frigate, host, tmp_path):
        b = make_bot(telegram, frigate, host, farm_status=write(tmp_path, doc()))
        send(b, "/farm", user=6666)
        assert "WS90" not in " ".join(telegram.texts())

    def test_env_turns_it_on(self):
        base = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_IDS": "1001"}
        cfg = bot.Config.from_env({**base, "FARM_STATUS_FILE": "/x/status.json"})
        assert cfg.farm_status == Path("/x/status.json")
        assert bot.Config.from_env(base).farm_status is None
