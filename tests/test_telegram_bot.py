"""telegram-bot/bot.py against a fake Telegram API and a fake Frigate."""

import importlib.util
import json
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


def message(user, text, date=None, chat=None):
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user},
            "chat": {"id": chat or user},
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
            f_blocks, f_bavail, f_frsize = 1000, 20, 4096

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
        assert {c["command"] for c in commands["commands"]} == {"status", "diag", "id", "help"}


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
