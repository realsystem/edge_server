"""telegram-bot/bot.py against a fake Telegram API and a fake Frigate."""

import importlib.util
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("edge_bot", REPO / "telegram-bot" / "bot.py")
bot = importlib.util.module_from_spec(spec)
sys.modules["edge_bot"] = bot
spec.loader.exec_module(bot)

TOKEN = "123456789:AAH" + "x" * 30
ALICE, EVE = 1001, 6666


class FakeServer:
    """Records requests; ``routes`` maps a path suffix to (status, body)."""

    def __init__(self, routes=None):
        self.calls = []  # (method name, payload dict | raw bytes, content-type)
        self.routes = routes or {}
        self.updates = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def respond(self, status, body):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.respond(*server.routes.get(self.path, (404, {})))

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                method = self.path.rsplit("/", 1)[-1]
                ctype = self.headers.get("Content-Type", "")
                payload = json.loads(body) if ctype == "application/json" else body
                server.calls.append((method, payload, ctype))
                if method in server.routes:
                    self.respond(*server.routes[method])
                elif method == "getUpdates":
                    updates, server.updates = server.updates, []
                    self.respond(200, {"ok": True, "result": updates})
                else:
                    self.respond(200, {"ok": True, "result": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def sent(self, method="sendMessage"):
        return [p for m, p, _ in self.calls if m == method]

    def texts(self):
        return [p["text"] for p in self.sent()]


FRIGATE_STATS = {
    "service": {"uptime": 90000},
    "detectors": {"cpu1": {"inference_speed": 87.5}},
    "cameras": {
        "front": {"camera_fps": 5.0, "detection_fps": 1.2, "skipped_fps": 0.0},
        "back": {"camera_fps": 5.0, "detection_fps": 0.0, "skipped_fps": 0.0},
    },
}


@pytest.fixture
def telegram():
    s = FakeServer()
    yield s
    s.close()


@pytest.fixture
def frigate():
    s = FakeServer(
        {
            "/api/version": (200, b"0.16.0"),
            "/api/stats": (200, FRIGATE_STATS),
            "/api/config": (200, {"cameras": {"front": {"enabled": True}}}),
        }
    )
    yield s
    s.close()


@pytest.fixture
def host(tmp_path):
    """A fake /proc and /sys plus app and storage directories."""
    proc, sysfs = tmp_path / "proc", tmp_path / "sys"
    proc.mkdir()
    (proc / "uptime").write_text("90061.5 1000.0\n")
    (proc / "loadavg").write_text("0.50 0.40 0.30 1/100 123\n")
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    (proc / "meminfo").write_text(
        "MemTotal: 8388608 kB\nMemAvailable: 6291456 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
    )
    zone = sysfs / "class/thermal/thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "type").write_text("x86_pkg_temp\n")
    (zone / "temp").write_text("61000\n")
    ac, bat = sysfs / "class/power_supply/AC", sysfs / "class/power_supply/BAT0"
    ac.mkdir(parents=True)
    bat.mkdir(parents=True)
    (ac / "type").write_text("Mains\n")
    (ac / "online").write_text("1\n")
    (bat / "type").write_text("Battery\n")
    (bat / "capacity").write_text("100\n")
    (bat / "status").write_text("Full\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "rec").mkdir()
    return tmp_path


def make_bot(telegram, frigate, host, **overrides):
    cfg = bot.Config(
        token=TOKEN,
        allowed=frozenset({ALICE}),
        frigate_url=frigate.url if frigate else "http://127.0.0.1:9",
        app_dir=host / "app",
        storage_path=host / "rec",
        proc=host / "proc",
        sys=host / "sys",
        api_url=telegram.url,
        diag_script=REPO / "collect-diagnostics.sh",
        diag_samples=1,
        diag_interval=1,
        diag_cooldown=60.0,
        **overrides,
    )
    return bot.Bot(cfg, bot.Telegram(TOKEN, telegram.url), sleep=lambda s: None)


def message(user, text, date=None, chat=None, chat_type="private"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 77,
            "from": {"id": user},
            "chat": {"id": chat or user, "type": chat_type},
            "text": text,
            "date": int(date or time.time()),
        },
    }


# ------------------------------------------------------------------ config


class TestConfig:
    ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_IDS": "1001, 2002 3003"}

    def test_parses_ids_and_defaults(self):
        cfg = bot.Config.from_env(self.ENV)
        assert cfg.allowed == {1001, 2002, 3003}
        assert cfg.frigate_url == "http://frigate:5000"

    @pytest.mark.parametrize("token", ["", "nope", "123:short", "abc:" + "x" * 30])
    def test_bad_token(self, token):
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            bot.Config.from_env({"TELEGRAM_BOT_TOKEN": token})

    def test_bad_id(self):
        with pytest.raises(ValueError, match="numeric"):
            bot.Config.from_env({**self.ENV, "TELEGRAM_ALLOWED_IDS": "12,@alice"})

    def test_empty_ids_allowed(self):
        assert bot.Config.from_env({"TELEGRAM_BOT_TOKEN": TOKEN}).allowed == frozenset()


# --------------------------------------------------------------- telegram api


class TestTelegramClient:
    def test_http_error_does_not_leak_token(self, telegram):
        telegram.routes["sendMessage"] = (401, {"ok": False, "description": "Unauthorized"})
        with pytest.raises(bot.TelegramError) as e:
            bot.Telegram(TOKEN, telegram.url).call("sendMessage", {})
        assert "401" in str(e.value) and TOKEN not in str(e.value) and e.value.code == 401

    def test_unreachable_does_not_leak_token(self):
        with pytest.raises(bot.TelegramError) as e:
            bot.Telegram(TOKEN, "http://127.0.0.1:9").call("getMe", timeout=2)
        assert TOKEN not in str(e.value)

    def test_not_ok_response(self, telegram):
        telegram.routes["getMe"] = (200, {"ok": False, "description": "bad"})
        with pytest.raises(bot.TelegramError, match="bad"):
            bot.Telegram(TOKEN, telegram.url).call("getMe")

    def test_send_document_is_multipart(self, telegram):
        bot.Telegram(TOKEN, telegram.url).send_document(42, "r.txt", b"hello", "cap")
        (body,) = telegram.sent("sendDocument")
        _, _, ctype = [c for c in telegram.calls if c[0] == "sendDocument"][0]
        assert ctype.startswith("multipart/form-data; boundary=")
        assert b'name="chat_id"\r\n\r\n42' in body
        assert b'filename="r.txt"' in body and b"hello" in body and b"cap" in body


# ------------------------------------------------------------- authorization


class TestAuthorization:
    def test_id_works_for_anyone(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(EVE, "/id"))
        assert telegram.texts() == [f"Your Telegram user id: {EVE}"]

    @pytest.mark.parametrize("cmd", ["/status", "/diag", "/whatever", "/status@my_bot"])
    def test_strangers_get_no_answer(self, telegram, frigate, host, cmd):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(EVE, cmd))
        assert telegram.calls == []

    def test_stranger_start_gets_instructions_only(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(EVE, "/start"))
        (text,) = telegram.texts()
        assert str(EVE) in text and "TELEGRAM_ALLOWED_IDS" in text
        assert "Frigate" not in text

    def test_authorization_is_by_sender_not_chat(self, telegram, frigate, host):
        # Eve writing in a group chat that has the same id as Alice's does not get in
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(EVE, "/status", chat=ALICE))
        assert telegram.calls == []

    def test_old_commands_ignored(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(ALICE, "/status", date=time.time() - 3600))
        assert telegram.calls == []

    def test_plain_text_and_bad_updates_ignored(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(ALICE, "hello"))
        b.handle_update({"update_id": 2, "message": {"text": "/status"}})
        b.handle_update({"update_id": 3, "edited_message": {}})
        assert telegram.calls == []

    def test_help(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(ALICE, "/help"))
        assert "/diag" in telegram.texts()[0] and "/status" in telegram.texts()[0]


# -------------------------------------------------------------------- status


class TestStatus:
    def test_healthy(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(ALICE, "/status@edge_bot"))
        (payload,) = telegram.sent()
        assert payload["parse_mode"] == "HTML"
        text = payload["text"]
        assert "Edge server: OK" in text
        assert "up 1d 1h" in text and "load 0.50 / 0.40 / 0.30" in text
        assert "memory 2.0 of 8.0 GB used" in text
        assert "61 C (x86_pkg_temp)" in text and "AC power" in text
        assert "Frigate 0.16.0, up 1d 1h" in text
        assert "detector cpu1: 87.5 ms" in text
        assert "front: 5 fps, detecting 1.2/s" in text

    def test_cpu_percent_from_two_samples(self, telegram, frigate, host):
        def second_sample(_):
            (host / "proc/stat").write_text("cpu  200 0 200 900 0 0 0 0 0 0\n")

        b = make_bot(telegram, frigate, host)
        b.sleep = second_sample
        assert "CPU 67%" in bot.build_status(b.cfg, b.sleep)  # 400 busy of 600

    def test_camera_without_video_is_a_problem(self, telegram, frigate, host):
        frigate.routes["/api/stats"] = (
            200,
            {**FRIGATE_STATS, "cameras": {"gate": {"camera_fps": 0.0}}},
        )
        text = bot.build_status(make_bot(telegram, frigate, host).cfg, lambda s: None)
        assert "1 problem(s)" in text and "gate: no video" in text and "gate: NO VIDEO" in text

    def test_disabled_camera_is_not_a_problem(self, telegram, frigate, host):
        # e.g. the placeholder camera in the generated config: enabled: false, 0 fps
        frigate.routes["/api/stats"] = (
            200,
            {"cameras": {"front": {"camera_fps": 5}, "placeholder": {"camera_fps": 0.0}}},
        )
        frigate.routes["/api/config"] = (
            200,
            {"cameras": {"front": {"enabled": True}, "placeholder": {"enabled": False}}},
        )
        text = bot.build_status(make_bot(telegram, frigate, host).cfg, lambda s: None)
        assert "Edge server: OK" in text and "placeholder: disabled" in text
        assert "NO VIDEO" not in text

    def test_unknown_config_still_flags_dead_camera(self, telegram, frigate, host):
        frigate.routes["/api/stats"] = (200, {"cameras": {"gate": {"camera_fps": 0.0}}})
        del frigate.routes["/api/config"]  # 404: cannot tell, so assume it should be running
        text = bot.build_status(make_bot(telegram, frigate, host).cfg, lambda s: None)
        assert "gate: no video" in text

    def test_storage_usage_has_its_own_line(self, telegram, frigate, host):
        cfg = make_bot(telegram, frigate, host).cfg
        text = bot.build_status(cfg, lambda s: None)
        lines = [line.strip() for line in text.splitlines()]
        storage = [line for line in lines if line.startswith("storage /mnt/storage:")]
        assert len(storage) == 1
        assert re.fullmatch(r"storage /mnt/storage: [\d.]+ of [\d.]+ GB used \(\d+%\)", storage[0])
        recordings = [i for i, line in enumerate(lines) if line.startswith("recordings:")]
        assert (
            recordings and lines[recordings[0] + 1] == storage[0]
        )  # right under the free-space line

    def test_storage_label_comes_from_the_environment(self):
        cfg = bot.Config.from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "STORAGE_LABEL": "/data/disk2"})
        assert cfg.storage_label == "/data/disk2"
        assert (
            bot.storage_usage_line("/data/disk2", Path("/nonexistent"))
            == "storage /data/disk2: n/a"
        )
        blank = bot.Config.from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "STORAGE_LABEL": " "})
        assert blank.storage_label == "/mnt/storage"

    def test_slow_detector_is_a_problem(self, telegram, frigate, host):
        stats = {"cameras": {"gate": {"camera_fps": 5, "skipped_fps": 2.5}}}
        frigate.routes["/api/stats"] = (200, stats)
        text = bot.build_status(make_bot(telegram, frigate, host).cfg, lambda s: None)
        assert "detector too slow" in text

    def test_frigate_down(self, telegram, host):
        text = bot.build_status(make_bot(telegram, None, host).cfg, lambda s: None)
        assert "NOT REACHABLE" in text and "Frigate is not reachable" in text

    def test_on_battery_and_hot(self, telegram, frigate, host):
        (host / "sys/class/power_supply/AC/online").write_text("0\n")
        (host / "sys/class/power_supply/BAT0/status").write_text("Discharging\n")
        (host / "sys/class/thermal/thermal_zone0/temp").write_text("95000\n")
        text = bot.build_status(make_bot(telegram, frigate, host).cfg, lambda s: None)
        assert "running on battery" in text and "very hot (95 C)" in text
        assert "ON BATTERY, battery 100% discharging" in text

    def test_missing_host_files_do_not_crash(self, telegram, frigate, tmp_path):
        cfg = bot.Config(
            token=TOKEN,
            frigate_url=frigate.url,
            proc=tmp_path / "none",
            sys=tmp_path / "none",
            app_dir=tmp_path / "none",
            storage_path=tmp_path / "none",
        )
        text = bot.build_status(cfg, lambda s: None)
        assert "Frigate 0.16.0" in text and "n/a" in text

    def test_low_disk(self, telegram, frigate, host, monkeypatch):
        class Full:
            f_blocks, f_bavail, f_bfree, f_frsize = 1000, 20, 20, 4096

        monkeypatch.setattr(bot.os, "statvfs", lambda p: Full())
        text = bot.build_status(make_bot(telegram, frigate, host).cfg, lambda s: None)
        assert "recordings almost full" in text and "(2%)" in text

    def test_html_is_escaped(self, telegram, frigate, host):
        frigate.routes["/api/stats"] = (
            200,
            {"cameras": {"<b>x&y</b>": {"camera_fps": 5}}},
        )
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(ALICE, "/status"))
        assert "&lt;b&gt;x&amp;y&lt;/b&gt;" in telegram.sent()[0]["text"]

    def test_long_text_is_split(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.reply(ALICE, "\n".join(f"line {i:04d} " + "x" * 60 for i in range(200)))
        assert len(telegram.texts()) > 1
        assert all(len(t) <= 4096 for t in telegram.texts())


# ---------------------------------------------------------------------- diag


class TestDiag:
    def run_diag(self, b):
        b.handle_update(message(ALICE, "/diag"))
        for t in b.threads:
            t.join(timeout=120)

    def test_sends_report_file(self, telegram, frigate, host, monkeypatch):
        (host / "app/.cam").write_text("192.168.1.20 admin:" + "pw" + "-secret1\n")
        (host / "app/frigate").mkdir()
        (host / "app/frigate/config.yml").write_text(
            "cameras:\n  front:\n    ffmpeg:\n      inputs:\n"
            '        - path: "rtsp://admin:pw-secret1@192.168.1.20:554/main"\n'
        )
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        b = make_bot(telegram, frigate, host)
        self.run_diag(b)
        assert "Collecting diagnostics" in telegram.texts()[0]
        (doc,) = telegram.sent("sendDocument")
        assert b'filename="edge-diagnostics-' in doc
        assert b"Edge server diagnostics" in doc and b"cameras:" in doc
        assert b"inference_speed=87.5" in doc  # from the fake Frigate API
        assert b"pw-secret1" not in doc and TOKEN.encode() not in doc

    def test_report_includes_the_hosts_firewall_snapshot(self, telegram, frigate, host):
        status = host / "status"
        status.mkdir()
        (status / "host-snapshot.txt").write_text("Host snapshot\n22/tcp ALLOW IN 192.168.0.0/16\n")
        b = make_bot(telegram, frigate, host, status_dir=status)
        self.run_diag(b)
        (doc,) = telegram.sent("sendDocument")
        assert b"Network, firewall and open ports (host)" in doc
        assert b"from the host snapshot" in doc and b"22/tcp ALLOW IN 192.168.0.0/16" in doc

    def test_cooldown_and_busy(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        self.run_diag(b)
        assert len(telegram.sent("sendDocument")) == 1
        telegram.calls.clear()
        self.run_diag(b)
        assert telegram.sent("sendDocument") == []
        assert "Please wait" in telegram.texts()[0]

    def test_concurrent_request_rejected(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        assert b._diag_lock.acquire(blocking=False)
        b.cmd_diag(ALICE)
        assert "already in progress" in telegram.texts()[0]
        assert telegram.sent("sendDocument") == []

    def test_failure_is_reported(self, telegram, frigate, host):
        b = make_bot(
            telegram,
            frigate,
            host,
        )
        b.cfg.diag_script = host / "missing.sh"
        self.run_diag(b)
        assert telegram.sent("sendDocument") == []
        assert any("failed" in t.lower() or "could not" in t.lower() for t in telegram.texts())


# ------------------------------------------------------------------- polling


class TestPolling:
    def test_offset_advances_and_updates_handled(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        telegram.updates = [{**message(ALICE, "/help"), "update_id": 41}]
        offset = b.poll_once(None, timeout=1)
        assert offset == 42
        assert "/diag" in telegram.texts()[0]
        b.poll_once(offset, timeout=1)
        polls = telegram.sent("getUpdates")
        assert "offset" not in polls[0] and polls[1]["offset"] == 42
        assert polls[0]["allowed_updates"] == ["message"]

    def test_handler_crash_does_not_stop_polling(self, telegram, frigate, host, monkeypatch):
        b = make_bot(telegram, frigate, host)
        monkeypatch.setattr(b, "cmd_status", lambda chat: 1 / 0)
        telegram.updates = [
            {**message(ALICE, "/status"), "update_id": 1},
            {**message(ALICE, "/help"), "update_id": 2},
        ]
        assert b.poll_once(None, timeout=1) == 3
        assert "/diag" in telegram.texts()[0]

    def test_run_registers_commands_and_backs_off(self, telegram, frigate, host):
        telegram.routes["getUpdates"] = (500, {"ok": False})
        b = make_bot(telegram, frigate, host)
        delays = []

        def stop(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise KeyboardInterrupt

        b.sleep = stop
        with pytest.raises(KeyboardInterrupt):
            b.run()
        assert delays == [2, 4, 8]
        (commands,) = telegram.sent("setMyCommands")
        assert {c["command"] for c in commands["commands"]} == {
            "status",
            "diag",
            "testalert",
            "id",
            "help",
        }


# ---------------------------------------------------------------------- main


class TestMain:
    def test_missing_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert bot.main([]) == 2

    def test_check_reports_missing_script(self, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("DIAG_SCRIPT", "/nonexistent/collect.sh")
        assert bot.main(["--check"]) == 1
        assert "missing /nonexistent/collect.sh" in capsys.readouterr().out

    def test_check_ok(self, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("DIAG_SCRIPT", str(REPO / "collect-diagnostics.sh"))
        monkeypatch.setattr(bot, "check_environment", lambda cfg: [])
        assert bot.main(["--check"]) == 0


# --------------------------------------------------------------- disk alerts


def fake_usage(monkeypatch, pct, total=100 * 2**30):
    """Make every watched disk report ``pct`` percent free (mutable via the list)."""
    state = [pct]
    monkeypatch.setattr(bot, "disk_usage", lambda path: (total, int(total * state[0] / 100)))
    return state


class TestDiskAlerts:
    def test_alert_once_remind_then_recover(self, telegram, frigate, host, monkeypatch):
        b = make_bot(telegram, frigate, host)
        pct = fake_usage(monkeypatch, 50)
        assert b.disk_alert_messages(now=0) == []

        pct[0] = 10  # crosses the 15% level
        (alert,) = b.disk_alert_messages(now=1000)
        assert "Low disk space" in alert and "10.0%" in alert and "alert level 15%" in alert
        assert "reminder" not in alert and "oldest recordings" in alert

        assert b.disk_alert_messages(now=1300) == []  # no repeat every 5 minutes
        (reminder,) = b.disk_alert_messages(now=1000 + b.cfg.alert_repeat)
        assert "(reminder)" in reminder

        pct[0] = 16  # above the level but not clearly recovered: stay quiet
        assert b.disk_alert_messages(now=1000 + b.cfg.alert_repeat + 1) == []
        pct[0] = 25
        (ok,) = b.disk_alert_messages(now=1000 + b.cfg.alert_repeat + 2)
        assert "recovered" in ok and "25%" in ok
        assert b.disk_alert_messages(now=1000 + b.cfg.alert_repeat + 3) == []

    def test_alerts_again_after_recovery(self, telegram, frigate, host, monkeypatch):
        b = make_bot(telegram, frigate, host)
        pct = fake_usage(monkeypatch, 5)
        assert len(b.disk_alert_messages(now=0)) == 1
        pct[0] = 60
        assert "recovered" in b.disk_alert_messages(now=1)[0]
        pct[0] = 5
        assert "Low disk space" in b.disk_alert_messages(now=2)[0]

    def test_turned_off_with_zero(self, telegram, frigate, host, monkeypatch):
        b = make_bot(telegram, frigate, host, alert_free_percent=0.0)
        fake_usage(monkeypatch, 1)
        assert b.disk_alert_messages(now=0) == []

    def test_unreadable_disk_is_ignored(self, telegram, frigate, host, monkeypatch):
        b = make_bot(telegram, frigate, host)
        monkeypatch.setattr(bot, "disk_usage", lambda path: None)
        assert b.disk_alert_messages(now=0) == []

    def test_one_alert_per_filesystem(self, telegram, frigate, host, monkeypatch):
        # recordings and the app dir are on the same filesystem here (both under tmp_path)
        b = make_bot(telegram, frigate, host)
        fake_usage(monkeypatch, 5)
        (alert,) = b.disk_alert_messages(now=0)
        assert alert.count("Low disk space") == 1
        assert "recordings and system disk" in alert

    def test_sent_to_every_authorized_user(self, telegram, frigate, host, monkeypatch):
        b = make_bot(telegram, frigate, host)
        b.cfg.allowed = frozenset({ALICE, 2002})
        fake_usage(monkeypatch, 5)
        b.send_alerts()
        assert sorted(p["chat_id"] for p in telegram.sent()) == [ALICE, 2002]
        assert all("Low disk space" in t for t in telegram.texts())

    def test_loop_sends_and_stops(self, telegram, frigate, host, monkeypatch):
        b = make_bot(
            telegram,
            frigate,
            host,
        )
        b.cfg.alert_interval = 0.01
        fake_usage(monkeypatch, 5)
        thread = threading.Thread(target=b.alert_loop, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while not telegram.texts() and time.time() < deadline:
            time.sleep(0.02)
        b.stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(telegram.texts()) == 1  # alerted once, not on every check

    def test_loop_survives_errors(self, telegram, frigate, host, monkeypatch):
        b = make_bot(telegram, frigate, host)
        b.cfg.alert_interval = 0.01
        calls = []

        def boom():
            calls.append(1)
            if len(calls) >= 3:
                b.stop.set()
            raise RuntimeError("disk exploded")

        monkeypatch.setattr(b, "send_alerts", boom)
        b.alert_loop()
        assert len(calls) == 3

    def test_run_starts_the_loop_only_when_useful(self, telegram, frigate, host, monkeypatch):
        started = []
        real_start = threading.Thread.start

        def tracking_start(self):
            name = getattr(self._target, "__name__", "")
            if name in ("alert_loop", "watch_loop"):
                started.append(name)  # do not really run it
            else:
                real_start(self)  # the fake HTTP servers need real threads

        monkeypatch.setattr(threading.Thread, "start", tracking_start)
        for allowed, percent, watch, expected in [
            (frozenset({ALICE}), 15.0, True, ["alert_loop", "watch_loop"]),
            (frozenset({ALICE}), 0.0, True, ["watch_loop"]),  # watchers alone are enough
            (frozenset({ALICE}), 15.0, False, ["alert_loop"]),
            (frozenset(), 15.0, True, []),
            (frozenset({ALICE}), 0.0, False, []),
        ]:
            started.clear()
            b = make_bot(telegram, frigate, host, alert_free_percent=percent, watch=watch)
            b.cfg.allowed = allowed
            b.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt)
            telegram.routes["getUpdates"] = (500, {"ok": False})
            with pytest.raises(KeyboardInterrupt):
                b.run()
            assert started == expected


class TestTestAlertCommand:
    def test_simulates_alert_for_requester_only(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.cfg.allowed = frozenset({ALICE, 2002})
        b.handle_update(message(ALICE, "/testalert"))
        first, second = telegram.sent()
        assert first["chat_id"] == second["chat_id"] == ALICE  # not sent to the other user
        assert "TEST - this is not a real alert" in first["text"]
        assert "Low disk space" in first["text"] and "5.0%" in first["text"]
        assert "Real state:" in second["text"] and "Alerts go to 2 user(s)" in second["text"]

    def test_custom_percent(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(ALICE, "/testalert 2.5"))
        assert "2.5%" in telegram.texts()[0]

    @pytest.mark.parametrize("arg", ["abc", "-1", "101"])
    def test_bad_argument(self, telegram, frigate, host, arg):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(ALICE, f"/testalert {arg}"))
        assert telegram.texts() == ["Usage: /testalert [free percent between 0 and 100]"]

    def test_says_when_alerts_are_off(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host, alert_free_percent=0.0)
        b.handle_update(message(ALICE, "/testalert"))
        assert "turned off" in telegram.texts()[1]

    def test_strangers_cannot_trigger_it(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(EVE, "/testalert"))
        assert telegram.calls == []

    def test_listed_in_help(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        b.handle_update(message(ALICE, "/help"))
        assert "/testalert" in telegram.texts()[0]


class TestAlertConfig:
    ENV = {"TELEGRAM_BOT_TOKEN": TOKEN}

    def test_defaults(self):
        cfg = bot.Config.from_env(self.ENV)
        assert (cfg.alert_free_percent, cfg.alert_interval, cfg.alert_repeat) == (
            15.0,
            300.0,
            21600.0,
        )

    def test_from_env(self):
        env = {
            **self.ENV,
            "ALERT_DISK_FREE_PERCENT": "20",
            "ALERT_INTERVAL": "60",
            "ALERT_REPEAT_HOURS": "1",
        }
        cfg = bot.Config.from_env(env)
        assert (cfg.alert_free_percent, cfg.alert_interval, cfg.alert_repeat) == (
            20.0,
            60.0,
            3600.0,
        )

    @pytest.mark.parametrize("value", ["lots", "-1", "101"])
    def test_invalid_percent(self, value):
        with pytest.raises(ValueError, match="ALERT_DISK_FREE_PERCENT"):
            bot.Config.from_env({**self.ENV, "ALERT_DISK_FREE_PERCENT": value})


# -------------------------------------------------------------------- reboot

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 test key "12345678901234567890"
NOW = 1_800_000_000.0


class TestTotp:
    @pytest.mark.parametrize(
        "t, code",
        [
            (59, "287082"),
            (1111111109, "081804"),
            (1111111111, "050471"),
            (1234567890, "005924"),
            (2000000000, "279037"),
            (20000000000, "353130"),
        ],
    )
    def test_rfc6238_vectors(self, t, code):
        assert bot.totp(RFC_SECRET, t // 30) == code

    def test_window_allows_one_step_of_drift(self):
        now = 1111111109
        step = now // 30
        for s in (step - 1, step, step + 1):
            assert bot.verify_totp(RFC_SECRET, bot.totp(RFC_SECRET, s), now) == s
        assert bot.verify_totp(RFC_SECRET, bot.totp(RFC_SECRET, step + 2), now) is None
        assert bot.verify_totp(RFC_SECRET, bot.totp(RFC_SECRET, step - 2), now) is None

    def test_a_step_works_only_once(self):
        now = 1111111109
        code = bot.totp(RFC_SECRET, now // 30)
        step = bot.verify_totp(RFC_SECRET, code, now)
        assert bot.verify_totp(RFC_SECRET, code, now, last_step=step) is None
        # an older code than the last used one is refused too
        older = bot.totp(RFC_SECRET, now // 30 - 1)
        assert bot.verify_totp(RFC_SECRET, older, now, last_step=step) is None

    def test_normalize_secret(self):
        assert bot.normalize_secret("") == ""
        assert bot.normalize_secret("  ") == ""
        assert bot.normalize_secret("gezd gnbv-gy3t qojq gezd gnbv gy3t qojq") == RFC_SECRET
        for bad in ("short", "GEZDGNBVGY3TQOJ1", "GEZDGNBVGY3TQOJQ!"):
            with pytest.raises(ValueError, match="REBOOT_TOTP_SECRET"):
                bot.normalize_secret(bad)

    def test_config_reads_secret_and_rejects_bad_ones(self):
        env = {"TELEGRAM_BOT_TOKEN": TOKEN}
        assert bot.Config.from_env(env).totp_secret == ""
        cfg = bot.Config.from_env(
            {**env, "REBOOT_TOTP_SECRET": RFC_SECRET.lower(), "REQUESTS_DIR": "/r"}
        )
        assert cfg.totp_secret == RFC_SECRET and cfg.requests_dir == Path("/r")
        with pytest.raises(ValueError):
            bot.Config.from_env({**env, "REBOOT_TOTP_SECRET": "nope"})

    def test_setup_prints_a_working_secret(self):
        lines = []
        assert bot.totp_setup(lines.append, rand=lambda n: bytes(range(n))) == 0
        text = "\n".join(lines)
        secret = text.split("REBOOT_TOTP_SECRET=")[1].split()[0]
        assert bot.normalize_secret(secret) == secret and len(secret) == 32
        assert "otpauth://totp/" in text and f"secret={secret}" in text
        assert bot.totp(secret, int(time.time() // 30)) in text


@pytest.fixture
def reboot(telegram, frigate, host):
    """A bot with /reboot on, a fake clock and an empty requests dir."""
    requests = host / "requests"
    requests.mkdir()
    b = make_bot(
        telegram,
        frigate,
        host,
        totp_secret=RFC_SECRET,
        requests_dir=requests,
        reboot_wait=3600,
    )
    b.clock = lambda: NOW
    b.sent_timers = []
    return b


def code_now(offset=0):
    return bot.totp(RFC_SECRET, int(NOW // 30) + offset)


def send(b, text, user=ALICE, **kw):
    kw.setdefault("date", NOW)
    b.handle_update(message(user, text, **kw))


class TestReboot:
    def test_success_requests_reboot_and_cleans_up(self, reboot, telegram):
        send(reboot, f"/reboot {code_now()}")
        req = reboot.cfg.requests_dir
        assert (req / "reboot").exists()
        marker = json.loads((req / "reboot-notify").read_text())
        assert marker == {"chat_id": ALICE, "at": NOW}
        assert (req / "totp-last-step").read_text() == str(int(NOW // 30))
        assert telegram.sent("deleteMessage") == [{"chat_id": ALICE, "message_id": 77}]
        assert "Rebooting now" in telegram.texts()[-1]
        reboot._reboot_timer.cancel()

    def test_replay_is_refused(self, reboot, telegram):
        code = code_now()
        send(reboot, f"/reboot {code}")
        reboot._reboot_timer.cancel()
        (reboot.cfg.requests_dir / "reboot").unlink()  # the host consumed the request
        send(reboot, f"/reboot {code}")
        assert "Wrong code" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "reboot").exists()

    def test_pending_request_is_not_duplicated(self, reboot, telegram):
        (reboot.cfg.requests_dir / "reboot").touch()
        send(reboot, f"/reboot {code_now()}")
        assert "already pending" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "totp-last-step").exists()

    def test_wrong_codes_lock_the_user_out(self, reboot, telegram):
        wrong = "000000" if code_now() != "000000" else "111111"
        for n in (1, 2):
            send(reboot, f"/reboot {wrong}")
            assert f"Wrong code ({n}/3)" in telegram.texts()[-1]
        send(reboot, f"/reboot {wrong}")
        assert "Locked for 15 minutes" in telegram.texts()[-1]
        send(reboot, f"/reboot {code_now()}")  # even the right code now
        assert "Too many wrong codes" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "reboot").exists()
        reboot.clock = lambda: NOW + 16 * 60
        send(reboot, f"/reboot {code_now(32)}", date=NOW + 16 * 60)
        assert (reboot.cfg.requests_dir / "reboot").exists()
        reboot._reboot_timer.cancel()

    def test_old_message_is_refused(self, reboot, telegram):
        send(reboot, f"/reboot {code_now()}", date=NOW - 120)
        assert "too old" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "reboot").exists()

    def test_malformed_code_does_not_count_as_a_failure(self, reboot, telegram):
        for _ in range(5):
            send(reboot, "/reboot abc")
        assert all("6 digits" in t for t in telegram.texts())
        send(reboot, f"/reboot {code_now()}")
        assert (reboot.cfg.requests_dir / "reboot").exists()
        reboot._reboot_timer.cancel()

    def test_no_argument_explains(self, reboot, telegram):
        send(reboot, "/reboot")
        assert "6-digit code" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "reboot").exists()

    def test_recently_booted_host_is_not_rebooted(self, reboot, telegram, host):
        (host / "proc" / "uptime").write_text("120.0 100.0\n")
        send(reboot, f"/reboot {code_now()}")
        assert "Not rebooting: uptime is" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "reboot").exists()

    def test_unreadable_uptime_is_not_rebooted(self, reboot, telegram, host):
        (host / "proc" / "uptime").unlink()
        send(reboot, f"/reboot {code_now()}")
        assert "uptime is unknown" in telegram.texts()[-1]

    def test_group_chats_are_refused(self, reboot, telegram):
        send(reboot, f"/reboot {code_now()}", chat=-500, chat_type="supergroup")
        assert "private chat" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "reboot").exists()

    def test_strangers_cannot_reboot(self, reboot, telegram):
        send(reboot, f"/reboot {code_now()}", user=EVE)
        assert telegram.texts() == []
        assert not (reboot.cfg.requests_dir / "reboot").exists()

    def test_not_configured(self, telegram, frigate, host):
        b = make_bot(telegram, frigate, host)
        send(b, "/reboot 123456")
        assert "not set up" in telegram.texts()[-1]

    def test_missing_requests_dir(self, reboot, telegram):
        reboot.cfg.requests_dir = reboot.cfg.requests_dir / "gone"
        send(reboot, f"/reboot {code_now()}")
        assert "not set up" in telegram.texts()[-1]

    def test_cannot_store_the_used_code(self, reboot, telegram, monkeypatch):
        def boom(step):
            raise OSError("read-only")

        monkeypatch.setattr(reboot, "_save_last_step", boom)
        send(reboot, f"/reboot {code_now()}")
        assert "cannot write" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "reboot").exists()

    def test_delete_failure_does_not_block_the_reboot(self, reboot, telegram):
        telegram.routes["deleteMessage"] = (400, {"ok": False, "description": "can't delete"})
        send(reboot, f"/reboot {code_now()}")
        assert (reboot.cfg.requests_dir / "reboot").exists()
        reboot._reboot_timer.cancel()

    def test_help_and_menu_mention_reboot_only_when_enabled(self, reboot, telegram, frigate, host):
        assert "/reboot" in reboot.help_text()
        assert "reboot" in [c["command"] for c in reboot.commands()]
        plain = make_bot(telegram, frigate, host)
        assert "/reboot" not in plain.help_text()
        assert "reboot" not in [c["command"] for c in plain.commands()]

    def test_timer_reports_a_reboot_that_never_happened(self, reboot, telegram):
        send(reboot, f"/reboot {code_now()}")
        reboot._reboot_timer.cancel()
        reboot._reboot_not_done(ALICE)
        assert "did not reboot" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "reboot").exists()
        assert not (reboot.cfg.requests_dir / "reboot-notify").exists()


class TestAfterReboot:
    def write_marker(self, b, at):
        (b.cfg.requests_dir / "reboot-notify").write_text(json.dumps({"chat_id": ALICE, "at": at}))

    def test_back_online_message(self, reboot, telegram, host):
        (host / "proc" / "uptime").write_text("45.0 40.0\n")
        self.write_marker(reboot, NOW - 100)
        reboot.notify_after_reboot()
        assert "Back online after reboot" in telegram.texts()[-1]
        assert not (reboot.cfg.requests_dir / "reboot-notify").exists()

    def test_bot_restart_without_reboot(self, reboot, telegram):
        self.write_marker(reboot, NOW - 100)  # host uptime in the fixture is a day
        reboot.notify_after_reboot()
        assert "did not reboot" in telegram.texts()[-1]

    def test_stale_marker_is_dropped_silently(self, reboot, telegram):
        self.write_marker(reboot, NOW - 7200)
        reboot.notify_after_reboot()
        assert telegram.texts() == []
        assert not (reboot.cfg.requests_dir / "reboot-notify").exists()

    def test_no_marker_and_broken_marker(self, reboot, telegram):
        reboot.notify_after_reboot()
        (reboot.cfg.requests_dir / "reboot-notify").write_text("not json")
        reboot.notify_after_reboot()
        assert telegram.texts() == []

    def test_retries_while_the_network_comes_up(self, reboot, telegram, host):
        (host / "proc" / "uptime").write_text("45.0 40.0\n")
        self.write_marker(reboot, NOW - 100)
        telegram.routes["sendMessage"] = (502, {"ok": False, "description": "bad gateway"})
        reboot.notify_after_reboot()
        assert len(telegram.sent()) == 6  # gave up after six tries

    def test_run_starts_the_notice_only_when_enabled(self, reboot, telegram, monkeypatch):
        started = []
        monkeypatch.setattr(reboot, "notify_after_reboot", lambda: started.append(1))
        monkeypatch.setattr(reboot, "poll_once", lambda offset: (_ for _ in ()).throw(SystemExit))
        with pytest.raises(SystemExit):
            reboot.run()
        time.sleep(0.2)
        assert started == [1]
        assert "reboot" in [c["command"] for c in telegram.sent("setMyCommands")[0]["commands"]]


class TestMainTotpSetup:
    def test_cli_prints_setup_without_a_token(self, monkeypatch, capsys):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert bot.main(["--totp-setup"]) == 0
        assert "REBOOT_TOTP_SECRET=" in capsys.readouterr().out


# ------------------------------------------------------------------ watchers

T0 = 1_000_000.0  # fake monotonic time for the watchers


def stats_with(front_fps=5.0, back_fps=5.0):
    return {
        "service": {"uptime": 9000},
        "detectors": {"cpu1": {"inference_speed": 87.5}},
        "cameras": {
            "front": {"camera_fps": front_fps, "detection_fps": 1.0, "skipped_fps": 0.0},
            "back": {"camera_fps": back_fps, "detection_fps": 1.0, "skipped_fps": 0.0},
        },
    }


@pytest.fixture
def watcher(telegram, frigate, host):
    """A bot whose watchers see healthy defaults; tests break one thing at a time."""
    (host / "status").mkdir()
    b = make_bot(
        telegram,
        frigate,
        host,
        status_dir=host / "status",
        alert_repeat=3600.0,
        alert_free_percent=0,
        alert_confirm=2,
    )
    b.clock = lambda: NOW
    return b


def run_checks(b, times, start=T0, step=300):
    """Run the watchers ``times`` times, ``step`` seconds apart; collect messages per run."""
    return [b.watch_messages(start + i * step) for i in range(times)]


class TestWatchFrigate:
    def test_healthy_says_nothing(self, watcher):
        assert run_checks(watcher, 3) == [[], [], []]

    def test_down_is_reported_after_two_checks_then_reminded_then_recovers(self, watcher, frigate):
        good_url = watcher.cfg.frigate_url
        watcher.cfg.frigate_url = "http://127.0.0.1:9"  # nothing listens here
        first, second, third = run_checks(watcher, 3)
        assert first == [] and third == []
        assert len(second) == 1 and "Frigate is not responding" in second[0]
        assert "/reboot" in second[0]
        # an hour later: a reminder
        assert "(reminder)" in watcher.watch_messages(T0 + 3600 + 600)[0]
        watcher.cfg.frigate_url = good_url
        assert watcher.watch_messages(T0 + 8000) == ["✅ Frigate: back to normal."]
        assert watcher.watch_messages(T0 + 8300) == []

    def test_a_restart_blip_is_not_reported(self, watcher):
        good_url = watcher.cfg.frigate_url
        watcher.cfg.frigate_url = "http://127.0.0.1:9"
        assert watcher.watch_messages(T0) == []
        watcher.cfg.frigate_url = good_url
        assert run_checks(watcher, 3, start=T0 + 300) == [[], [], []]  # not even "recovered"

    def test_camera_offline_and_back(self, watcher, frigate):
        frigate.routes["/api/stats"] = (200, stats_with(front_fps=0.0))
        first, second = run_checks(watcher, 2)
        assert first == []
        assert len(second) == 1 and "Camera front has no video" in second[0]
        frigate.routes["/api/stats"] = (200, stats_with())
        assert watcher.watch_messages(T0 + 900) == ["✅ Camera front: back to normal."]

    def test_disabled_cameras_are_ignored(self, watcher, frigate):
        frigate.routes["/api/stats"] = (200, stats_with(front_fps=0.0))
        frigate.routes["/api/config"] = (200, {"cameras": {"front": {"enabled": False}}})
        assert run_checks(watcher, 3) == [[], [], []]

    def test_no_camera_alerts_while_frigate_is_down(self, watcher, frigate):
        frigate.routes["/api/stats"] = (200, stats_with(front_fps=0.0))
        watcher.watch_messages(T0)  # front: strike 1
        watcher.cfg.frigate_url = "http://127.0.0.1:9"
        messages = watcher.watch_messages(T0 + 300)
        assert all("Camera" not in m for m in messages)


class TestWatchTemperature:
    def set_temp(self, host, milli):
        (host / "sys/class/thermal/thermal_zone0/temp").write_text(f"{milli}\n")

    def test_hot_cpu_alerts_after_two_checks_and_recovers_with_margin(self, watcher, host):
        self.set_temp(host, 90000)
        first, second = run_checks(watcher, 2)
        assert first == []
        assert len(second) == 1 and "90 °C" in second[0] and "x86_pkg_temp" in second[0]
        self.set_temp(host, 82000)  # below the level but inside the 5 degree margin
        assert watcher.watch_messages(T0 + 900) == []
        self.set_temp(host, 70000)
        assert watcher.watch_messages(T0 + 1200) == ["✅ CPU temperature: back to normal."]

    def test_normal_temperature_is_quiet(self, watcher):
        assert run_checks(watcher, 3) == [[], [], []]  # fixture: 61 C

    def test_can_be_switched_off(self, watcher, host):
        self.set_temp(host, 99000)
        watcher.cfg.alert_temp_c = 0
        assert run_checks(watcher, 3) == [[], [], []]

    def test_no_sensor_is_quiet(self, watcher, host):
        (host / "sys/class/thermal/thermal_zone0/temp").unlink()
        assert run_checks(watcher, 3) == [[], [], []]


def write_disks(b, disks, generated=NOW, **extra):
    (b.cfg.status_dir / "disks.json").write_text(
        json.dumps({"generated": generated, "disks": disks, **extra})
    )


GOOD_DISK = {
    "device": "sda",
    "model": "Samsung SSD 860",
    "passed": True,
    "wear_used": 12,
    "reallocated": 0,
    "pending": 0,
    "media_errors": 0,
}


class TestWatchDiskHealth:
    def test_no_report_means_nothing_installed(self, watcher):
        assert run_checks(watcher, 3) == [[], [], []]

    def test_healthy_disk_is_quiet(self, watcher):
        write_disks(watcher, [GOOD_DISK])
        assert run_checks(watcher, 3) == [[], [], []]

    def test_worn_ssd_alerts_at_once_and_recovers(self, watcher):
        write_disks(watcher, [{**GOOD_DISK, "wear_used": 83}])
        messages = watcher.watch_messages(T0)
        assert len(messages) == 1
        assert "sda (Samsung SSD 860)" in messages[0] and "83% of its rated life" in messages[0]
        assert watcher.watch_messages(T0 + 300) == []  # no repeat inside the reminder period
        write_disks(watcher, [GOOD_DISK])
        assert watcher.watch_messages(T0 + 600) == [
            "✅ Disk sda (Samsung SSD 860): back to normal."
        ]

    def test_smart_failure_and_bad_sectors(self, watcher):
        write_disks(watcher, [{**GOOD_DISK, "passed": False, "reallocated": 8, "pending": 2}])
        (message,) = watcher.watch_messages(T0)
        assert "FAILED" in message and "8 reallocated, 2 pending" in message

    def test_wear_level_is_configurable_and_can_be_off(self, watcher):
        write_disks(watcher, [{**GOOD_DISK, "wear_used": 60}])
        assert watcher.watch_messages(T0) == []
        watcher.cfg.alert_wear_percent = 50
        assert len(watcher.watch_messages(T0 + 300)) == 1

    def test_stale_report_means_the_monitor_died(self, watcher):
        write_disks(watcher, [GOOD_DISK], generated=NOW - 4 * 86400)
        (message,) = watcher.watch_messages(T0)
        assert "has not been checked for" in message and "edge-smart" in message
        write_disks(watcher, [GOOD_DISK])
        assert watcher.watch_messages(T0 + 300) == ["✅ Disk health monitor: back to normal."]

    def test_monitor_error_is_reported(self, watcher):
        write_disks(watcher, [], error="smartctl is not installed or failed")
        (message,) = watcher.watch_messages(T0)
        assert "smartctl is not installed" in message

    def test_unreadable_report_is_reported_not_fatal(self, watcher):
        (watcher.cfg.status_dir / "disks.json").write_text("{nope")
        (message,) = watcher.watch_messages(T0)
        assert "unreadable" in message

    def test_disk_problems_helper(self):
        assert bot.disk_problems(GOOD_DISK, 80) == []
        assert bot.disk_problems({"wear_used": None, "media_errors": 3}, 80) == ["3 media errors"]
        assert bot.disk_problems({"wear_used": 90}, 0) == []  # wear alert off


class TestWatchIntegration:
    def test_send_alerts_delivers_watcher_messages_to_every_user(self, watcher, telegram, host):
        watcher.cfg.allowed = frozenset({ALICE, 2002})
        write_disks(watcher, [{**GOOD_DISK, "wear_used": 90}])
        watcher.send_watch_alerts()
        recipients = sorted(p["chat_id"] for p in telegram.sent())
        assert recipients == [ALICE, 2002]
        assert "90% of its rated life" in telegram.texts()[0]

    def test_watchers_can_be_switched_off(self, watcher, telegram):
        write_disks(watcher, [{**GOOD_DISK, "wear_used": 90}])
        watcher.cfg.watch = False
        watcher.send_watch_alerts()
        assert telegram.texts() == []

    def test_a_broken_check_does_not_hide_the_others(self, watcher, monkeypatch):
        write_disks(watcher, [{**GOOD_DISK, "wear_used": 90}])

        def boom(now):
            raise RuntimeError("bug")

        monkeypatch.setattr(watcher, "watch_temperature", boom)
        monkeypatch.setattr(
            bot.Bot, "watch_frigate", lambda self, now: (_ for _ in ()).throw(OSError())
        )
        assert len(watcher.watch_messages(T0)) == 1

    def test_system_disk_is_measured_through_the_frigate_mount(self, watcher, host):
        assert bot.system_path(watcher.cfg) == host / "app"
        (host / "app" / "frigate").mkdir()
        assert bot.system_path(watcher.cfg) == host / "app" / "frigate"
        assert bot.watched_disks(watcher.cfg)

    def test_config_from_env(self):
        env = {"TELEGRAM_BOT_TOKEN": TOKEN}
        cfg = bot.Config.from_env(env)
        assert cfg.watch and cfg.alert_confirm == 3 and cfg.watch_interval == 60.0
        assert cfg.alert_temp_c == 85.0
        assert cfg.alert_wear_percent == 80.0 and cfg.status_dir == Path("/status")
        cfg = bot.Config.from_env(
            {
                **env,
                "ALERT_WATCH": "false",
                "ALERT_TEMP_C": "90",
                "ALERT_DISK_WEAR_PERCENT": "70",
                "ALERT_CONFIRM": "0",
                "ALERT_WATCH_INTERVAL": "1",
                "STATUS_DIR": "/s",
            }
        )
        assert not cfg.watch and cfg.alert_temp_c == 90 and cfg.alert_wear_percent == 70
        assert cfg.alert_confirm == 1 and cfg.status_dir == Path("/s")
        assert cfg.watch_interval == 5.0  # not faster than every 5 s
        with pytest.raises(ValueError, match="must be numbers"):
            bot.Config.from_env({**env, "ALERT_TEMP_C": "hot"})


class TestCameraGrouping:
    def all_dark(self, frigate):
        frigate.routes["/api/stats"] = (200, stats_with(front_fps=0.0, back_fps=0.0))

    def test_cameras_that_die_together_get_one_message(self, watcher, frigate):
        self.all_dark(frigate)
        results = run_checks(watcher, 2)
        assert results[0] == []
        (message,) = results[1]
        assert message.startswith("⚠️ 2 cameras have no video: back, front.")
        assert "switch" in message

    def test_one_camera_keeps_the_single_camera_wording(self, watcher, frigate):
        frigate.routes["/api/stats"] = (200, stats_with(front_fps=0.0))
        (message,) = run_checks(watcher, 2)[1]
        assert message.startswith("⚠️ Camera front has no video")

    def test_reminder_and_recovery_are_grouped_too(self, watcher, frigate):
        self.all_dark(frigate)
        run_checks(watcher, 2)
        (reminder,) = watcher.watch_messages(T0 + 4000)
        assert "2 cameras have no video" in reminder and reminder.endswith("(reminder)")
        frigate.routes["/api/stats"] = (200, stats_with())
        assert watcher.watch_messages(T0 + 4060) == ["✅ Cameras back to normal: back, front."]

    def test_a_second_camera_failing_later_is_reported_on_its_own(self, watcher, frigate):
        frigate.routes["/api/stats"] = (200, stats_with(front_fps=0.0))
        run_checks(watcher, 2)
        self.all_dark(frigate)
        later = [watcher.watch_messages(T0 + 600 + i * 60) for i in range(2)]
        (message,) = later[1]
        assert message.startswith("⚠️ Camera back has no video")  # front is not repeated

    def test_the_switched_off_cameras_scenario_with_default_timing(self, telegram, frigate, host):
        """Four cameras dark at once: one alert about two minutes after the first check."""
        stats = stats_with()
        stats["cameras"] = {
            f"reolink_{n}": {"camera_fps": 0.0, "detection_fps": 0.0, "skipped_fps": 0.0}
            for n in (3, 4, 5, 6)
        }
        frigate.routes["/api/stats"] = (200, stats)
        frigate.routes["/api/config"] = (200, {"cameras": {}})
        b = make_bot(telegram, frigate, host, alert_free_percent=0)
        assert (b.cfg.alert_confirm, b.cfg.watch_interval) == (3, 60.0)
        sent_at = [i * b.cfg.watch_interval for i in range(5) if b.watch_messages(T0 + i * 60.0)]
        assert sent_at == [120.0]  # third check, i.e. two minutes after the first


# ------------------------------------------------------------------ redeploy


@pytest.fixture
def redeploy(reboot, monkeypatch):
    """/reboot's setup plus a recorder instead of the real waiting thread."""
    reboot.watches = []
    monkeypatch.setattr(reboot, "_start_redeploy_watch", lambda chat: reboot.watches.append(chat))
    return reboot


def req(b, name):
    return b.cfg.requests_dir / name


def write_result(b, **data):
    data.setdefault("at", NOW)
    req(b, "redeploy-result").write_text(json.dumps(data))


class TestRedeployCommand:
    def test_accepted_code_requests_a_redeploy(self, redeploy, telegram):
        send(redeploy, f"/redeploy {code_now()}")
        assert req(redeploy, "redeploy").exists()
        assert json.loads(req(redeploy, "redeploy-notify").read_text()) == {
            "chat_id": ALICE,
            "at": NOW,
        }
        assert req(redeploy, "totp-last-step").read_text() == str(int(NOW // 30))
        assert telegram.sent("deleteMessage") == [{"chat_id": ALICE, "message_id": 77}]
        assert "Deploying the newest release tag" in telegram.texts()[-1]
        assert redeploy.watches == [ALICE]

    def test_a_code_works_once_across_both_commands(self, redeploy, telegram):
        code = code_now()
        send(redeploy, f"/redeploy {code}")
        req(redeploy, "redeploy").unlink()  # the host picked it up
        req(redeploy, "redeploy-notify").unlink()
        send(redeploy, f"/reboot {code}")
        assert "Wrong code" in telegram.texts()[-1]
        assert not req(redeploy, "reboot").exists()
        send(redeploy, f"/redeploy {code}")
        assert "Wrong code" in telegram.texts()[-1]

    def test_wrong_codes_lock_out_both_commands_together(self, redeploy, telegram):
        wrong = "000000" if code_now() != "000000" else "111111"
        send(redeploy, f"/reboot {wrong}")
        send(redeploy, f"/redeploy {wrong}")
        send(redeploy, f"/reboot {wrong}")
        assert "Locked for 15 minutes" in telegram.texts()[-1]
        send(redeploy, f"/redeploy {code_now()}")
        assert "Too many wrong codes" in telegram.texts()[-1]
        assert not req(redeploy, "redeploy").exists()

    def test_nothing_happens_without_the_code(self, redeploy, telegram):
        send(redeploy, "/redeploy")
        assert "6-digit code" in telegram.texts()[-1]
        send(redeploy, "/redeploy 12ab56")
        assert "6 digits" in telegram.texts()[-1]
        send(redeploy, f"/redeploy {code_now()}", date=NOW - 120)
        assert "too old" in telegram.texts()[-1]
        assert not req(redeploy, "redeploy").exists()

    def test_only_listed_users_in_private_chats_and_only_when_set_up(
        self, redeploy, telegram, frigate, host
    ):
        send(redeploy, f"/redeploy {code_now()}", user=EVE)
        assert telegram.texts() == []
        send(redeploy, f"/redeploy {code_now()}", chat=-5, chat_type="group")
        assert "private chat" in telegram.texts()[-1]
        plain = make_bot(telegram, frigate, host)
        send(plain, "/redeploy 123456")
        assert "not set up" in telegram.texts()[-1]
        assert not req(redeploy, "redeploy").exists()

    def test_not_while_one_is_running(self, redeploy, telegram):
        req(redeploy, "redeploy-notify").write_text(json.dumps({"chat_id": ALICE, "at": NOW - 60}))
        send(redeploy, f"/redeploy {code_now()}")
        assert "already in progress" in telegram.texts()[-1]
        assert not req(redeploy, "redeploy").exists()
        assert not req(redeploy, "totp-last-step").exists()  # the code was not burned

    def test_an_old_marker_does_not_block(self, redeploy, telegram):
        req(redeploy, "redeploy-notify").write_text(
            json.dumps({"chat_id": ALICE, "at": NOW - 7200})
        )
        send(redeploy, f"/redeploy {code_now()}")
        assert req(redeploy, "redeploy").exists()

    def test_not_while_a_reboot_is_pending(self, redeploy, telegram):
        req(redeploy, "reboot").touch()
        send(redeploy, f"/redeploy {code_now()}")
        assert "reboot is pending" in telegram.texts()[-1]

    def test_an_old_result_is_cleared_first(self, redeploy):
        write_result(redeploy, status="ok", **{"from": "a", "to": "b"})
        send(redeploy, f"/redeploy {code_now()}")
        assert not req(redeploy, "redeploy-result").exists()

    def test_menu_and_help_list_redeploy_only_when_enabled(self, redeploy, telegram, frigate, host):
        assert "redeploy" in [c["command"] for c in redeploy.commands()]
        assert "/redeploy <code>" in redeploy.help_text()
        plain = make_bot(telegram, frigate, host)
        assert "redeploy" not in [c["command"] for c in plain.commands()]
        assert "/redeploy" not in plain.help_text()


class TestRedeployResult:
    def test_delivered_once_to_the_requester(self, reboot, telegram):
        req(reboot, "redeploy-notify").write_text(json.dumps({"chat_id": ALICE, "at": NOW}))
        write_result(reboot, status="ok", **{"from": "a" * 40, "to": "b" * 40})
        assert reboot.deliver_redeploy_result() is True
        assert telegram.texts() == [
            "✅ Redeployed: updated aaaaaaa → bbbbbbb. All containers are running."
        ]
        assert not req(reboot, "redeploy-notify").exists()
        assert not req(reboot, "redeploy-result").exists()
        assert not req(reboot, "redeploy-result.claimed").exists()
        assert reboot.deliver_redeploy_result() is False

    def test_without_a_marker_everyone_allowed_is_told(self, reboot, telegram):
        reboot.cfg.allowed = frozenset({ALICE, 2002})
        write_result(reboot, status="ok", **{"from": "a" * 40, "to": "a" * 40})
        reboot.deliver_redeploy_result()
        assert sorted(p["chat_id"] for p in telegram.sent()) == [ALICE, 2002]
        assert "Already on the latest version (aaaaaaa)" in telegram.texts()[0]

    def test_a_result_from_long_ago_is_dropped_quietly(self, reboot, telegram):
        write_result(reboot, status="ok", at=NOW - 7200)
        assert reboot.deliver_redeploy_result() is True
        assert telegram.texts() == []

    @pytest.mark.parametrize(
        "data,words",
        [
            (
                {
                    "status": "rolled_back",
                    "from": "a" * 9,
                    "to": "b" * 9,
                    "detail": "Frigate did not come up",
                },
                ["update to bbbbbbb failed", "Frigate did not come up", "Rolled back to aaaaaaa"],
            ),
            (
                {"status": "refused", "detail": "CI failed on the new version"},
                ["Redeploy not started: CI failed on the new version."],
            ),
            (
                {"status": "failed", "detail": "the deploy script failed (exit 2)"},
                ["❌", "exit 2", "rollback did not work", "edge-server-deploy.log"],
            ),
            ({}, ["❌", "unknown reason"]),
        ],
    )
    def test_wording(self, data, words):
        text = bot.format_redeploy_result(data)
        for w in words:
            assert w in text

    def test_result_names_the_release_tag(self):
        text = bot.format_redeploy_result(
            {"status": "ok", "from": "a" * 40, "to": "b" * 40, "tag": "v1.2"}
        )
        assert "release v1.2 (bbbbbbb)" in text and "aaaaaaa" in text

    def test_garbled_result_is_still_reported(self, reboot, telegram):
        req(reboot, "redeploy-result").write_text("{nope")
        assert reboot.deliver_redeploy_result() is True
        assert "❌" in telegram.texts()[-1]


class TestRedeployWatching:
    def setup_cfg(self, b):
        b.cfg.redeploy_poll, b.cfg.redeploy_start_wait, b.cfg.redeploy_max_wait = 1.0, 3.0, 10.0
        req(b, "redeploy-notify").write_text(json.dumps({"chat_id": ALICE, "at": NOW}))

    def test_waits_until_the_result_appears(self, reboot, telegram):
        self.setup_cfg(reboot)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 4:  # the host finished
                write_result(reboot, status="ok", **{"from": "a" * 7, "to": "b" * 7})

        reboot.sleep = sleep
        reboot._watch_redeploy(ALICE)
        assert len(sleeps) == 4 and "Redeployed: updated" in telegram.texts()[-1]

    def test_reports_a_request_the_host_never_picked_up(self, reboot, telegram):
        self.setup_cfg(reboot)
        req(reboot, "redeploy").touch()
        reboot._watch_redeploy(ALICE)
        assert "did not start" in telegram.texts()[-1]
        assert "edge-redeploy.path" in telegram.texts()[-1]
        assert not req(reboot, "redeploy").exists() and not req(reboot, "redeploy-notify").exists()

    def test_gives_up_after_the_maximum_wait(self, reboot, telegram):
        self.setup_cfg(reboot)  # the request was consumed, but no result ever comes
        reboot._watch_redeploy(ALICE)
        assert "No redeploy result after" in telegram.texts()[-1]
        assert not req(reboot, "redeploy-notify").exists()

    def test_resume_at_start_up(self, redeploy, telegram):
        # finished while the bot was being recreated
        req(redeploy, "redeploy-notify").write_text(json.dumps({"chat_id": ALICE, "at": NOW}))
        write_result(redeploy, status="ok", **{"from": "a" * 7, "to": "b" * 7})
        redeploy.resume_redeploy()
        assert "Redeployed" in telegram.texts()[-1] and redeploy.watches == []
        # still running: keep waiting for the requester
        req(redeploy, "redeploy-notify").write_text(json.dumps({"chat_id": ALICE, "at": NOW - 30}))
        redeploy.resume_redeploy()
        assert redeploy.watches == [ALICE]

    def test_resume_with_nothing_to_do(self, redeploy, telegram):
        redeploy.resume_redeploy()
        assert redeploy.watches == [] and telegram.texts() == []

    def test_run_resumes_a_redeploy(self, redeploy, telegram, monkeypatch):
        resumed = []
        monkeypatch.setattr(redeploy, "resume_redeploy", lambda: resumed.append(1))
        monkeypatch.setattr(redeploy, "poll_once", lambda offset: (_ for _ in ()).throw(SystemExit))
        with pytest.raises(SystemExit):
            redeploy.run()
        assert resumed == [1]


# ------------------------------------------------------------ restart and logs

COMPOSE = """\
services:
  frigate:
    image: x
    environment:
      - A=1
  mosquitto:
    image: y
  telegram-bot:
    image: z
volumes:
  data: {}
"""


@pytest.fixture
def jobs(reboot, telegram, monkeypatch):
    """/reboot's setup, a stack with three services and a recorder for the waiting thread."""
    (reboot.cfg.app_dir / "docker-compose.yml").write_text(COMPOSE)
    reboot.watches = []
    monkeypatch.setattr(
        reboot,
        "_start_job_watch",
        lambda kind, chat, wait, deliver: reboot.watches.append((kind, chat)),
    )
    return reboot


class TestComposeServices:
    def test_reads_the_services_block_only(self, jobs):
        assert bot.compose_services(jobs.cfg) == ["frigate", "mosquitto", "telegram-bot"]

    def test_missing_or_odd_file(self, jobs):
        (jobs.cfg.app_dir / "docker-compose.yml").unlink()
        assert bot.compose_services(jobs.cfg) == []
        (jobs.cfg.app_dir / "docker-compose.yml").write_text("volumes:\n  frigate:\n")
        assert bot.compose_services(jobs.cfg) == []


class TestRestartCommand:
    def test_accepted_code_requests_a_restart(self, jobs, telegram):
        send(jobs, f"/restart frigate {code_now()}")
        assert json.loads(req(jobs, "restart-request").read_text()) == {"service": "frigate"}
        assert req(jobs, "totp-last-step").read_text() == str(int(NOW // 30))
        assert telegram.sent("deleteMessage") == [{"chat_id": ALICE, "message_id": 77}]
        assert "Restarting frigate" in telegram.texts()[-1]
        assert jobs.watches == [("restart", ALICE)]
        assert not list(jobs.cfg.requests_dir.glob(".*.tmp"))  # no temp file left behind

    def test_service_name_is_case_insensitive(self, jobs):
        send(jobs, f"/restart FRIGATE {code_now()}")
        assert json.loads(req(jobs, "restart-request").read_text())["service"] == "frigate"

    def test_unknown_service_spends_no_code(self, jobs, telegram):
        send(jobs, f"/restart nginx {code_now()}")
        assert "Unknown service 'nginx'" in telegram.texts()[-1]
        assert "frigate, mosquitto" in telegram.texts()[-1]
        assert not req(jobs, "restart-request").exists()
        assert not req(jobs, "totp-last-step").exists()  # the code is still good
        send(jobs, f"/restart frigate {code_now()}")
        assert req(jobs, "restart-request").exists()

    def test_the_bot_cannot_restart_itself(self, jobs, telegram):
        send(jobs, f"/restart telegram-bot {code_now()}")
        assert "Unknown service" in telegram.texts()[-1]
        assert "telegram-bot" not in telegram.texts()[-1].split("Services:")[1]
        assert not req(jobs, "restart-request").exists()

    @pytest.mark.parametrize("text", ["/restart", "/restart frigate"])
    def test_usage_lists_the_services(self, jobs, telegram, text):
        send(jobs, text)
        assert "/restart <service> <code>" in telegram.texts()[-1]
        assert "frigate, mosquitto" in telegram.texts()[-1]
        assert not req(jobs, "restart-request").exists()

    def test_wrong_code_is_counted_and_nothing_happens(self, jobs, telegram):
        wrong = "000000" if code_now() != "000000" else "111111"
        send(jobs, f"/restart frigate {wrong}")
        assert "Wrong code (1/3)" in telegram.texts()[-1]
        assert not req(jobs, "restart-request").exists() and jobs.watches == []

    def test_a_code_works_once_across_all_commands(self, jobs, telegram):
        code = code_now()
        send(jobs, f"/restart frigate {code}")
        req(jobs, "restart-request").unlink()  # the host picked it up
        jobs._active_jobs.clear()
        send(jobs, f"/restart frigate {code}")
        assert "Wrong code" in telegram.texts()[-1]
        send(jobs, f"/reboot {code}")
        assert "Wrong code" in telegram.texts()[-1]

    def test_only_one_restart_at_a_time(self, jobs, telegram):
        send(jobs, f"/restart frigate {code_now()}")
        send(jobs, f"/restart mosquitto {code_now(1)}")
        assert "already in progress" in telegram.texts()[-1]
        assert json.loads(req(jobs, "restart-request").read_text())["service"] == "frigate"

    def test_not_while_a_reboot_or_redeploy_is_pending(self, jobs, telegram):
        for pending in ("reboot", "redeploy"):
            req(jobs, pending).touch()
            send(jobs, f"/restart frigate {code_now()}")
            assert f"A {pending} is pending" in telegram.texts()[-1]
            req(jobs, pending).unlink()
        assert not req(jobs, "restart-request").exists()

    def test_needs_the_authenticator_private_chat_and_a_listed_user(
        self, jobs, telegram, frigate, host
    ):
        send(jobs, f"/restart frigate {code_now()}", chat_type="group")
        assert "private chat" in telegram.texts()[-1]
        send(jobs, f"/restart frigate {code_now()}", user=999)
        assert not req(jobs, "restart-request").exists()
        plain = make_bot(telegram, frigate, host)  # no TOTP secret
        send(plain, "/restart frigate 123456")
        assert "not set up" in telegram.texts()[-1]

    def test_a_failure_to_write_the_request_is_reported(self, jobs, telegram, monkeypatch):
        def broken(kind, payload):
            raise OSError("read-only file system")

        monkeypatch.setattr(jobs, "_submit_job", broken)
        send(jobs, f"/restart frigate {code_now()}")
        assert "Could not request the restart" in telegram.texts()[-1]
        assert jobs.watches == []


class TestRestartResult:
    @pytest.mark.parametrize(
        "data,words",
        [
            ({"status": "ok", "service": "frigate"}, ["✅", "Restarted frigate", "running again"]),
            (
                {"status": "ok", "service": "frigate", "detail": "API not answering yet"},
                ["✅", "API not answering yet"],
            ),
            (
                {"status": "failed", "service": "frigate", "detail": "not running"},
                ["❌", "frigate failed", "not running", "/logs frigate"],
            ),
            ({"status": "refused", "detail": "unknown service"}, ["not done", "unknown service"]),
            ({}, ["❌", "unknown reason"]),
        ],
    )
    def test_wording(self, data, words):
        text = bot.format_restart_result(data)
        for w in words:
            assert w in text

    def test_delivery_claims_the_result_once(self, jobs, telegram):
        assert jobs._deliver_restart(ALICE) is False
        req(jobs, "restart-result").write_text(json.dumps({"status": "ok", "service": "frigate"}))
        assert jobs._deliver_restart(ALICE) is True
        assert "Restarted frigate" in telegram.texts()[-1]
        assert jobs._deliver_restart(ALICE) is False
        assert not list(jobs.cfg.requests_dir.glob("restart-result*"))

    def test_garbled_result_is_still_reported(self, jobs, telegram):
        req(jobs, "restart-result").write_text("{nope")
        assert jobs._deliver_restart(ALICE) is True
        assert "❌" in telegram.texts()[-1]


class TestJobWatching:
    def setup_cfg(self, b):
        b.cfg.job_poll, b.cfg.job_start_wait = 1.0, 3.0

    def deliver_after(self, b, calls):
        state = {"n": 0}

        def deliver(chat):
            state["n"] += 1
            return state["n"] >= calls

        return deliver, state

    def test_waits_until_the_host_answers(self, jobs):
        self.setup_cfg(jobs)
        deliver, state = self.deliver_after(jobs, 4)
        jobs._active_jobs.add("restart")
        jobs._watch_job("restart", ALICE, 10.0, deliver)
        assert state["n"] == 4 and "restart" not in jobs._active_jobs

    def test_reports_a_request_the_host_never_picked_up(self, jobs, telegram):
        self.setup_cfg(jobs)
        req(jobs, "restart-request").touch()
        jobs._watch_job("restart", ALICE, 10.0, lambda chat: False)
        assert "did not pick up the restart" in telegram.texts()[-1]
        assert "edge-restart.path" in telegram.texts()[-1]
        assert not req(jobs, "restart-request").exists()

    def test_gives_up_after_the_maximum_wait(self, jobs, telegram):
        self.setup_cfg(jobs)
        jobs._watch_job("logs", ALICE, 10.0, lambda chat: False)
        assert "No answer to /logs after" in telegram.texts()[-1]

    def test_a_real_watch_thread_delivers(self, reboot, telegram):
        """No fakes: command -> request file -> (host answers) -> message."""
        (reboot.cfg.app_dir / "docker-compose.yml").write_text(COMPOSE)
        reboot.cfg.job_poll = 0.01
        send(reboot, f"/restart frigate {code_now()}")
        req(reboot, "restart-request").unlink()  # the host picked it up
        req(reboot, "restart-result").write_text(json.dumps({"status": "ok", "service": "frigate"}))
        for t in reboot.threads:
            t.join(timeout=10)
        assert "Restarted frigate" in telegram.texts()[-1]
        assert "restart" not in reboot._active_jobs


class TestLogsCommand:
    def test_requests_the_logs(self, jobs, telegram):
        send(jobs, "/logs frigate 50")
        assert json.loads(req(jobs, "logs-request").read_text()) == {
            "service": "frigate",
            "lines": 50,
        }
        assert jobs.watches == [("logs", ALICE)]
        assert telegram.sent("deleteMessage") == []  # no code involved

    def test_default_is_100_lines_and_the_bot_itself_is_allowed(self, jobs):
        send(jobs, "/logs telegram-bot")
        assert json.loads(req(jobs, "logs-request").read_text()) == {
            "service": "telegram-bot",
            "lines": 100,
        }

    @pytest.mark.parametrize(
        "text", ["/logs", "/logs nginx", "/logs frigate 0", "/logs frigate 1001", "/logs frigate x"]
    )
    def test_bad_arguments_get_the_usage(self, jobs, telegram, text):
        send(jobs, text)
        assert "Usage: /logs <service> [lines 1-1000]" in telegram.texts()[-1]
        assert "frigate, mosquitto, telegram-bot" in telegram.texts()[-1]
        assert not req(jobs, "logs-request").exists()

    def test_cooldown_and_busy(self, jobs, telegram):
        send(jobs, "/logs frigate")
        req(jobs, "logs-request").unlink()
        send(jobs, "/logs mosquitto")
        assert "Please wait" in telegram.texts()[-1]
        jobs.clock = lambda: NOW + 60
        req(jobs, "logs-request").touch()
        send(jobs, "/logs mosquitto", date=NOW + 60)
        assert "already being fetched" in telegram.texts()[-1]

    def test_not_set_up_without_the_authenticator(self, telegram, frigate, host):
        plain = make_bot(telegram, frigate, host)
        send(plain, "/logs frigate")
        assert "/logs is not set up" in telegram.texts()[-1]

    def test_only_listed_users(self, jobs, telegram):
        send(jobs, "/logs frigate", user=999)
        assert not req(jobs, "logs-request").exists()


class TestLogsResult:
    def answer(self, b, text, **result):
        result = {"status": "ok", "service": "frigate", "lines": 100, **result}
        req(b, "logs-result").write_text(json.dumps(result))
        if text is not None:
            req(b, "logs-output.txt").write_text(text)

    def test_short_logs_are_sent_as_a_message(self, jobs, telegram):
        self.answer(jobs, "2026-01-01 frigate | started\n")
        assert jobs._deliver_logs(ALICE) is True
        assert "frigate (last 100 lines)" in telegram.texts()[-1]
        assert "frigate | started" in telegram.texts()[-1]
        assert telegram.sent("sendDocument") == []
        assert not req(jobs, "logs-output.txt").exists()  # cleaned up

    def test_long_logs_are_sent_as_a_file(self, jobs, telegram):
        self.answer(jobs, "line of log text\n" * 400)
        assert jobs._deliver_logs(ALICE) is True
        (doc,) = telegram.sent("sendDocument")
        assert b'filename="frigate-logs-' in doc and b"line of log text" in doc
        assert b"Passwords are removed" in doc
        assert not req(jobs, "logs-output.txt").exists()

    def test_empty_logs(self, jobs, telegram):
        self.answer(jobs, "")
        jobs._deliver_logs(ALICE)
        assert "no log lines" in telegram.texts()[-1]

    def test_refusal_and_failure_from_the_host(self, jobs, telegram):
        self.answer(jobs, None, status="refused", detail="unknown service 'x'")
        jobs._deliver_logs(ALICE)
        assert "Logs not available: unknown service 'x'" in telegram.texts()[-1]

    def test_no_answer_yet(self, jobs):
        assert jobs._deliver_logs(ALICE) is False


class TestHelpAndMenu:
    def test_new_commands_only_with_the_authenticator(self, jobs, telegram, frigate, host):
        names = {c["command"] for c in jobs.commands()}
        assert {"restart", "logs"} <= names
        assert (
            "/restart <service> <code>" in jobs.help_text()
            and "/logs <service>" in jobs.help_text()
        )
        plain = make_bot(telegram, frigate, host)
        assert not {"restart", "logs"} & {c["command"] for c in plain.commands()}
        assert "/restart" not in plain.help_text()
