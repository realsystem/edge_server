"""tools/kuma_provision.py against a fake Uptime Kuma (the real Socket.IO channel needs a live server)."""

import copy
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import kuma_provision as kp

TOKEN = "fake-bot-token-used-only-in-these-tests"
CHAT = "123456789"
URL = "https://monitor.example.com"


class FakeKuma:
    """Answers the Socket.IO events the tool uses, the way Kuma 2 does."""

    def __init__(
        self, needs_setup=True, admin=None, monitors=None, notifications=None, test_ok=True
    ):
        self.needs_setup = needs_setup
        self.admin = admin
        self._monitors: dict[str, dict[str, Any]] = {str(m["id"]): m for m in (monitors or [])}
        self._notifications: list[dict[str, Any]] = list(notifications or [])
        self.test_ok = test_ok
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def call(self, event, *args):
        self.calls.append((event, args))
        if event == "needSetup":
            return self.needs_setup
        if event == "setup":
            self.admin = (args[0], args[1])
            self.needs_setup = False
            return {"ok": True}
        if event == "login":
            ok = (args[0]["username"], args[0]["password"]) == self.admin
            return {"ok": ok, "msg": "Incorrect username or password."}
        if event == "testNotification":
            return {"ok": self.test_ok, "msg": f"chat not found for {args[0]['telegramBotToken']}"}
        if event == "addNotification":
            note = {
                "id": len(self._notifications) + 1,
                "name": args[0]["name"],
                "isDefault": args[0]["isDefault"],
            }
            self._notifications.append(note)
            return {"ok": True, "id": note["id"]}
        if event == "add":
            monitor = dict(args[0], id=len(self._monitors) + 1)
            self._monitors[str(monitor["id"])] = monitor
            return {"ok": True, "monitorID": monitor["id"]}
        if event == "getMonitor":
            return {"ok": True, "monitor": copy.deepcopy(self._monitors[str(args[0])])}
        if event == "editMonitor":
            self._monitors[str(args[0]["id"])] = args[0]
            return {"ok": True}
        raise AssertionError(f"unexpected event {event}")

    def settle(self):
        pass

    def monitors(self):
        return self._monitors

    def notifications(self):
        return self._notifications

    def close(self):
        self.closed = True

    def events(self):
        return [event for event, _ in self.calls]


class FakeHttp:
    def __init__(self, entry_types=("entryPage",), post_ok=True):
        self.entry_types = list(entry_types)
        self.posts: list[tuple[str, dict]] = []
        self.post_ok = post_ok

    def get_json(self, path):
        assert path == "/api/entry-page"
        kind = self.entry_types.pop(0) if len(self.entry_types) > 1 else self.entry_types[0]
        return {"type": kind}

    def post_json(self, path, body):
        self.posts.append((path, body))
        return {"ok": self.post_ok}


@pytest.fixture
def files(tmp_path):
    token = tmp_path / "telegram-token"
    token.write_text(TOKEN + "\n", encoding="utf-8")
    return {
        "password": tmp_path / "kuma-admin",
        "token": token,
        "urls": tmp_path / "heartbeat-urls.env",
    }


def run(files, kuma, http=None, *extra, monitors=("Home HA heartbeat", "Farm HA heartbeat")):
    argv = [
        "--url", "http://127.0.0.1:3001", "--public-url", URL,
        "--admin-password-file", str(files["password"]),
        "--telegram-token-file", str(files["token"]), "--telegram-chat-id", CHAT,
        "--urls-out", str(files["urls"]),
    ]  # fmt: skip
    for name in monitors:
        argv += ["--monitor", name]
    return kp.main(
        argv + list(extra),
        kuma_factory=lambda url: kuma,
        http_factory=lambda base: http or FakeHttp(),
    )


def test_a_fresh_server_gets_an_admin_a_notification_and_the_monitors(files, capsys):
    kuma = FakeKuma()
    assert run(files, kuma) == 0
    password = files["password"].read_text().strip()
    assert len(password) == 28 and kuma.admin == ("admin", password)
    assert [n["name"] for n in kuma.notifications()] == ["Telegram"] and kuma.notifications()[0][
        "isDefault"
    ]
    monitors = list(kuma.monitors().values())
    assert [m["name"] for m in monitors] == ["Home HA heartbeat", "Farm HA heartbeat"]
    assert all(
        m["type"] == "push" and m["interval"] == 900 and m["maxretries"] == 0 for m in monitors
    )
    assert all(
        m["notificationIDList"] == {"1": True} for m in monitors
    )  # the default notification is attached
    assert kuma.closed
    out = capsys.readouterr().out
    assert (
        "created the admin account" in out and "added the push monitor 'Home HA heartbeat'" in out
    )


def test_the_push_urls_go_to_a_private_file_and_never_to_the_screen(files, capsys):
    kuma = FakeKuma()
    run(files, kuma)
    lines = dict(line.split("=", 1) for line in files["urls"].read_text().splitlines())
    token = kuma.monitors()["1"]["pushToken"]
    assert (
        lines["HOME_HA_HEARTBEAT_URL"] == f"{URL}/api/push/{token}"
    )  # the public address, not the loopback one
    assert set(lines) == {"HOME_HA_HEARTBEAT_URL", "FARM_HA_HEARTBEAT_URL"}
    assert len(token) == 32 and token.isalnum()
    if os.name != "nt":
        assert stat.S_IMODE(files["urls"].stat().st_mode) == 0o600
        assert stat.S_IMODE(files["password"].stat().st_mode) == 0o600
    shown = capsys.readouterr()
    for secret in (TOKEN, files["password"].read_text().strip(), token):
        assert secret not in shown.out + shown.err


def test_running_it_again_changes_nothing(files, capsys):
    kuma = FakeKuma()
    run(files, kuma)
    before = (
        files["urls"].read_text(),
        files["password"].read_text(),
        copy.deepcopy(kuma.monitors()),
    )
    kuma.calls.clear()
    capsys.readouterr()
    assert run(files, kuma) == 0
    assert set(kuma.events()) <= {"needSetup", "login"}  # nothing was added, edited or set up
    assert (files["urls"].read_text(), files["password"].read_text(), kuma.monitors()) == before
    out = capsys.readouterr().out
    assert "already exists" in out and "already right" in out


def test_plan_mode_changes_nothing_and_creates_no_files(files, capsys):
    kuma = FakeKuma()
    assert run(files, kuma, None, "--plan") == 0
    assert kuma.events() == ["needSetup"]  # it could not even log in: there is no admin yet
    assert not files["password"].exists() and not files["urls"].exists()
    out = capsys.readouterr().out
    assert (
        "plan: would create the admin password file" in out
        and "plan: would create the admin account" in out
    )


def test_plan_mode_on_a_configured_server_lists_only_the_difference(files, capsys):
    kuma = FakeKuma()
    run(files, kuma, None, monitors=("Home HA heartbeat",))
    kuma.calls.clear()
    capsys.readouterr()
    assert run(files, kuma, None, "--plan") == 0
    assert set(kuma.events()) <= {"needSetup", "login"}
    out = capsys.readouterr().out
    assert "plan: would add the push monitor 'Farm HA heartbeat'" in out
    assert "Home HA heartbeat" in out and "already right" in out
    assert "Farm HA heartbeat" not in files["urls"].read_text()  # plan wrote nothing


def test_a_failing_telegram_test_stops_before_anything_is_added_and_hides_the_token(files, capsys):
    kuma = FakeKuma(test_ok=False)
    assert run(files, kuma) == 1
    assert kuma.notifications() == [] and kuma.monitors() == {}
    err = capsys.readouterr().err
    assert "Telegram test message failed" in err and "<token>" in err and TOKEN not in err


def test_the_test_message_can_be_skipped(files):
    kuma = FakeKuma(test_ok=False)
    assert run(files, kuma, None, "--no-test-message") == 0
    assert "testNotification" not in kuma.events()


def test_a_wrong_admin_password_is_reported(files, capsys):
    files["password"].write_text("not-the-password\n")
    kuma = FakeKuma(needs_setup=False, admin=("admin", "something-else"))
    assert run(files, kuma) == 1
    assert "cannot log in as 'admin'" in capsys.readouterr().err
    assert kuma.monitors() == {}


def test_an_existing_monitor_with_the_wrong_interval_is_corrected_and_keeps_its_token(files):
    old = {
        "id": 7,
        "name": "Home HA heartbeat",
        "type": "push",
        "pushToken": "K" * 32,
        "interval": 60,
        "notificationIDList": {},
    }
    kuma = FakeKuma(
        needs_setup=False,
        admin=("admin", "pw"),
        monitors=[old],
        notifications=[{"id": 3, "name": "Telegram", "isDefault": True}],
    )
    files["password"].write_text("pw\n")
    assert run(files, kuma, None, monitors=("Home HA heartbeat",)) == 0
    fixed = kuma.monitors()["7"]
    assert (fixed["interval"], fixed["retryInterval"], fixed["pushToken"]) == (900, 900, "K" * 32)
    assert fixed["notificationIDList"] == {"3": True}
    assert "addNotification" not in kuma.events()  # the notification existed
    assert files["urls"].read_text() == f"HOME_HA_HEARTBEAT_URL={URL}/api/push/{'K' * 32}\n"


def test_a_monitor_of_another_type_is_not_touched(files, capsys):
    other = {"id": 1, "name": "Home HA heartbeat", "type": "http", "interval": 60}
    kuma = FakeKuma(needs_setup=False, admin=("admin", "pw"), monitors=[other])
    files["password"].write_text("pw\n")
    assert run(files, kuma, None, monitors=("Home HA heartbeat",)) == 1
    assert "not a push monitor" in capsys.readouterr().err
    assert kuma.monitors()["1"] == other


def test_other_lines_in_the_urls_file_are_kept(files):
    files["urls"].write_text("# my urls\nOTHER_URL=https://elsewhere.example.com/x\n")
    run(files, FakeKuma(), None, monitors=("Home HA heartbeat",))
    text = files["urls"].read_text()
    assert "OTHER_URL=https://elsewhere.example.com/x" in text and "HOME_HA_HEARTBEAT_URL=" in text


def test_the_database_screen_is_answered_with_sqlite_and_waited_for(files, capsys):
    http = FakeHttp(entry_types=("setup-database", "setup-database", "entryPage"))
    assert run(files, FakeKuma(), http) == 0
    assert http.posts == [("/setup-database", {"dbConfig": {"type": "sqlite"}})]
    assert "chose SQLite" in capsys.readouterr().out


def test_plan_mode_stops_at_a_pending_database_screen_without_connecting(files, capsys):
    http = FakeHttp(entry_types=("setup-database",))

    def no_connection(url):
        raise AssertionError("must not connect before the database is chosen")

    argv = [
        "--url",
        "http://127.0.0.1:3001",
        "--admin-password-file",
        str(files["password"]),
        "--plan",
    ]
    assert kp.main(argv, kuma_factory=no_connection, http_factory=lambda base: http) == 0
    assert http.posts == [] and "would choose SQLite" in capsys.readouterr().out


def test_a_server_that_never_finishes_its_first_start_times_out():
    http = FakeHttp(entry_types=("setup-database",))
    now = iter(range(0, 10_000, 50))
    with pytest.raises(kp.KumaError, match="first start"):
        kp.ensure_database(
            http, True, kp.Report(), sleep=lambda s: None, clock=lambda: float(next(now))
        )


def test_a_refused_database_choice_is_an_error():
    with pytest.raises(kp.KumaError, match="refused the database"):
        kp.ensure_database(
            FakeHttp(entry_types=("setup-database",), post_ok=False), True, kp.Report()
        )


def test_names_become_environment_keys_and_tokens_are_random():
    assert kp.env_key("Home HA heartbeat") == "HOME_HA_HEARTBEAT_URL"
    assert kp.env_key("  farm / NAS #2 ") == "FARM_NAS_2_URL"
    assert kp.new_token() != kp.new_token()


def test_a_tiny_interval_is_refused(files, capsys):
    assert run(files, FakeKuma(), None, "--interval", "30") == 1
    assert "at least 60" in capsys.readouterr().err


def test_an_empty_secret_file_is_an_error(tmp_path):
    empty = Path(tmp_path / "empty")
    empty.write_text("\n")
    with pytest.raises(kp.KumaError, match="is empty"):
        kp.read_secret(empty)


def test_the_wrapper_script_parses_and_keeps_its_dependency_in_a_venv():
    import shutil
    import subprocess

    script = Path(__file__).resolve().parent.parent / "ops" / "monitor" / "kuma-provision.sh"
    text = script.read_text(encoding="utf-8")
    assert (
        text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text and "\r" not in text
    )
    assert "python-socketio[client]" in text and "-m venv" in text  # never installed system-wide
    bash = shutil.which("bash")
    if bash:
        assert subprocess.run([bash, "-n", str(script)], capture_output=True).returncode == 0
