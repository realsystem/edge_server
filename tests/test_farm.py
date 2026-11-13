# ruff: noqa: F811, F401, E402 - pytest fixtures are imported from test_telegram_bot and used as arguments
"""/farm (Autonomy M10): what the Field Station cell last saw. Read only; stdlib only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))
import farm
from test_telegram_bot import NOW, T0, TOKEN, bot, frigate, host, make_bot, send, telegram


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


class TestRules:
    def test_parse(self):
        rules, bad = farm.parse_rules(
            "sensor.wh51_ch1_soil_moisture<20, sensor.ws90_temperature>40.5;nonsense, sensor.x<"
        )
        assert [r.text for r in rules] == [
            "sensor.wh51_ch1_soil_moisture<20",
            "sensor.ws90_temperature>40.5",
        ]
        assert bad == ["nonsense", "sensor.x<"]
        assert farm.parse_rules("") == ([], [])

    def test_low_rule_with_hysteresis(self):
        rule = farm.Rule("sensor.m", "<", 20)
        d = lambda v: {
            "entities": {"sensor.m": {"state": v, "unit": "%", "name": "Soil ch1"}}
        }  # noqa: E731
        assert farm.rule_state(rule, d("15")) == ("bad", "Soil ch1 is low: 15 % (alert below 20).")
        assert farm.rule_state(rule, d("20.5"))[0] == "band"  # not far enough back to end an alert
        assert farm.rule_state(rule, d("21"))[0] == "ok"

    def test_high_rule_with_hysteresis(self):
        rule = farm.Rule("sensor.t", ">", 40)
        d = lambda v: {
            "entities": {"sensor.t": {"state": v, "unit": "°C", "name": "Air"}}
        }  # noqa: E731
        assert farm.rule_state(rule, d("41"))[0] == "bad"
        assert farm.rule_state(rule, d("39"))[0] == "band"
        assert farm.rule_state(rule, d("38"))[0] == "ok"

    def test_missing_and_unusable_readings_are_problems(self):
        rule = farm.Rule("sensor.m", "<", 20)
        assert farm.rule_state(rule, {"entities": {}})[0] == "bad"
        out = farm.rule_state(
            rule, {"entities": {"sensor.m": {"state": "unavailable", "name": "Soil"}}}
        )
        assert out[0] == "bad" and "no usable reading (unavailable)" in out[1]

    def test_station_problem(self):
        assert farm.station_problem(
            None, NOW
        ) and "no readable status file" in farm.station_problem(None, NOW)
        assert "not reporting for 10 min" in farm.station_problem(doc(t=NOW - 600), NOW)
        assert "cannot reach Home Assistant" in farm.station_problem(doc(connected=False), NOW)
        assert farm.station_problem(doc(), NOW) is None


class TestWatcher:
    def bot(self, telegram, frigate, host, tmp_path, **kw):
        (host / "status").mkdir(exist_ok=True)
        self.path = tmp_path / "status.json"
        b = make_bot(
            telegram,
            frigate,
            host,
            status_dir=host / "status",
            alert_repeat=3600.0,
            alert_free_percent=0,
            alert_confirm=2,
            farm_status=self.path,
            **kw,
        )
        b.clock = lambda: NOW
        return b

    def farm_messages(self, b, now):
        return [
            m for m in b.watch_messages(now) if "Field Station" in m or "Soil" in m or "WS90" in m
        ]

    def test_healthy_station_says_nothing(self, telegram, frigate, host, tmp_path):
        b = self.bot(telegram, frigate, host, tmp_path)
        write(tmp_path, doc())
        assert [self.farm_messages(b, T0 + i * 300) for i in range(4)] == [[], [], [], []]

    def test_station_stops_reporting_then_recovers(self, telegram, frigate, host, tmp_path):
        b = self.bot(telegram, frigate, host, tmp_path)
        write(tmp_path, doc(t=NOW - 3600))  # last word an hour ago
        first, second = self.farm_messages(b, T0), self.farm_messages(b, T0 + 300)
        assert first == [] and len(second) == 1 and "not reporting for 60 min" in second[0]
        write(tmp_path, doc())  # the cell is back
        assert self.farm_messages(b, T0 + 600) == ["✅ Field Station: back to normal."]

    def test_missing_file_is_an_alert_too(self, telegram, frigate, host, tmp_path):
        b = self.bot(telegram, frigate, host, tmp_path)
        out = [self.farm_messages(b, T0 + i * 300) for i in range(2)]
        assert out[0] == [] and "no readable status file" in out[1][0]

    def test_home_assistant_unreachable(self, telegram, frigate, host, tmp_path):
        b = self.bot(telegram, frigate, host, tmp_path, farm_alert_confirm=1)
        write(tmp_path, doc(connected=False))
        assert "cannot reach Home Assistant" in self.farm_messages(b, T0)[0]

    def test_station_alert_can_be_switched_off(self, telegram, frigate, host, tmp_path):
        b = self.bot(
            telegram, frigate, host, tmp_path, farm_alert_station=False, farm_alert_confirm=1
        )
        write(tmp_path, doc(connected=False))
        assert self.farm_messages(b, T0) == []

    def test_thresholds(self, telegram, frigate, host, tmp_path):
        b = self.bot(
            telegram,
            frigate,
            host,
            tmp_path,
            farm_alert_confirm=1,
            farm_alerts="sensor.wh51_ch1_soil_moisture<30, sensor.ws90_temperature>40",
        )
        write(tmp_path, doc())  # soil 41 %, temperature 35: fine
        assert self.farm_messages(b, T0) == []
        d = doc()
        d["entities"]["sensor.wh51_ch1_soil_moisture"]["state"] = "25"
        write(tmp_path, d)
        low = self.farm_messages(b, T0 + 60)
        assert len(low) == 1 and "Soil moisture ch1 is low: 25 % (alert below 30)" in low[0]
        assert self.farm_messages(b, T0 + 120) == []  # one alert, not one per check
        d["entities"]["sensor.wh51_ch1_soil_moisture"]["state"] = "31"  # inside the hysteresis band
        write(tmp_path, d)
        assert self.farm_messages(b, T0 + 180) == []
        d["entities"]["sensor.wh51_ch1_soil_moisture"]["state"] = "35"
        write(tmp_path, d)
        assert self.farm_messages(b, T0 + 240) == ["✅ Soil moisture ch1: back to normal."]

    def test_a_blip_is_not_an_alert(self, telegram, frigate, host, tmp_path):
        b = self.bot(
            telegram, frigate, host, tmp_path, farm_alerts="sensor.wh51_ch1_soil_moisture<30"
        )  # confirm 2
        d = doc()
        d["entities"]["sensor.wh51_ch1_soil_moisture"]["state"] = "10"
        write(tmp_path, d)
        assert self.farm_messages(b, T0) == []
        write(tmp_path, doc())
        assert self.farm_messages(b, T0 + 300) == [] and self.farm_messages(b, T0 + 600) == []

    def test_a_bad_rule_is_logged_not_fatal(self, telegram, frigate, host, tmp_path, caplog):
        b = self.bot(
            telegram, frigate, host, tmp_path, farm_alerts="oops, sensor.ws90_temperature>40"
        )
        write(tmp_path, doc())
        b.watch_messages(T0)
        assert "cannot read 'oops'" in caplog.text
        assert [r.text for r in b._farm_rules] == ["sensor.ws90_temperature>40"]

    def test_stale_readings_are_not_judged(self, telegram, frigate, host, tmp_path):
        """A cell that is not reporting: one alert about the cell, not about its old numbers."""
        b = self.bot(
            telegram,
            frigate,
            host,
            tmp_path,
            farm_alert_confirm=1,
            farm_alerts="sensor.wh51_ch1_soil_moisture<99",
        )
        write(tmp_path, doc(t=NOW - 3600))
        out = self.farm_messages(b, T0)
        assert len(out) == 1 and "not reporting" in out[0]

    def test_off_without_a_cell(self, telegram, frigate, host):
        (host / "status").mkdir()
        b = make_bot(
            telegram,
            frigate,
            host,
            status_dir=host / "status",
            alert_free_percent=0,
            farm_alerts="sensor.x<1",
        )
        b.clock = lambda: NOW
        assert b.watch_farm(T0) == [] and not any(
            "Field Station" in m for m in b.watch_messages(T0)
        )

    def test_env(self):
        base = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_IDS": "1001"}
        cfg = bot.Config.from_env(
            {
                **base,
                "FARM_STATUS_FILE": "/s.json",
                "FARM_ALERTS": "a.b<1",
                "FARM_ALERT_CONFIRM": "1",
                "FARM_STALE_SECONDS": "30",
                "FARM_ALERT_STATION": "false",
            }
        )
        assert (
            cfg.farm_alerts,
            cfg.farm_alert_confirm,
            cfg.farm_stale,
            cfg.farm_alert_station,
        ) == ("a.b<1", 1, 30.0, False)
        default = bot.Config.from_env({**base, "FARM_STATUS_FILE": "/s.json"})
        assert (
            default.farm_alerts,
            default.farm_alert_confirm,
            default.farm_stale,
            default.farm_alert_station,
        ) == ("", 0, 120.0, True)
