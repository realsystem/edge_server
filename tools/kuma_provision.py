#!/usr/bin/env python3
"""Provision an Uptime Kuma server from code: database, admin account, Telegram notification, push monitors.

Kuma has no setup API, only the Socket.IO channel its web page uses. This tool drives that channel, so a new site's
monitor is one command instead of a list of clicks, and running it again changes nothing that is already right.

What one run does (each step only if it is missing or different):
  1. Kuma 2 starts at a "which database?" screen: choose SQLite (the light one; MariaDB needs far more memory than a
     small VM has).
  2. Create the admin account (the password comes from a file, or a random one is generated into that file, mode 600).
  3. Add a Telegram notification, set as default and applied to existing monitors (the bot token comes from a file).
  4. Add one *Push* monitor per --monitor NAME (heartbeat interval --interval, no retries), attach the default
     notifications, and save each monitor's push URL to --urls-out (mode 600). The push URL is a secret: it is never
     printed, only written to that file.

Usage:
  kuma_provision.py --url http://127.0.0.1:3001 --public-url https://monitor.example.com \\
      --admin-password-file /root/.kuma-admin \\
      --telegram-token-file /root/.kuma-telegram-token --telegram-chat-id-file /root/.kuma-telegram-chat \\
      --monitor "Home HA heartbeat" --monitor "Farm HA heartbeat" --urls-out /root/heartbeat-urls.env
  kuma_provision.py ... --plan        # say what would change, change nothing

Run it on the VM against Kuma's loopback address (--url): the admin page of a public monitor is usually limited to your own
address, and the VM's address is not on that list. --public-url is only used to build the push URLs. The Socket.IO part
needs `python-socketio[client]` and `websocket-client`; ops/monitor/kuma-provision.sh installs them into a venv and runs
this file. Everything else is the standard library.

Exit status: 0 ok, 1 something failed (the message says what), 2 the Socket.IO library is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

TOKEN_ALPHABET = string.ascii_letters + string.digits
SETTLE_SECONDS = 2.5  # Kuma registers its handlers a moment after the socket connects
DB_WAIT_SECONDS = 180.0


class KumaError(RuntimeError):
    """Something Kuma refused, or an answer that makes no sense."""


class Kuma(Protocol):
    def call(self, event: str, *args: Any) -> Any: ...

    def settle(self) -> None: ...

    def monitors(self) -> dict[str, dict[str, Any]]: ...

    def notifications(self) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class Http(Protocol):
    def get_json(self, path: str) -> Any: ...

    def post_json(self, path: str, body: dict[str, Any]) -> Any: ...


@dataclass
class Config:
    url: str
    public_url: str
    user: str
    password_file: Path
    monitors: list[str]
    interval: int = 900
    telegram_token_file: Path | None = None
    telegram_chat_id: str = ""
    notification_name: str = "Telegram"
    urls_out: Path | None = None
    plan: bool = False
    test_notification: bool = True


@dataclass
class Report:
    """What was (or, with --plan, would be) done, one line each. Never contains a secret."""

    lines: list[str] = field(default_factory=list)
    failed: bool = False

    def add(self, text: str) -> None:
        self.lines.append(text)


# ---------------------------------------------------------------------------------------------------------------------
# small helpers


def new_token(n: int = 32) -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(n))


def env_key(name: str) -> str:
    """'Home HA heartbeat' -> 'HOME_HA_HEARTBEAT_URL'."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() + "_URL"


def read_secret(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise KumaError(f"{path} is empty")
    return text


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read, never leaving it world-readable even for a moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - a file system without modes
        pass


def admin_password(path: Path, plan: bool, report: Report) -> str | None:
    if path.exists():
        return read_secret(path)
    if plan:
        report.add(f"would create the admin password file {path} (random, mode 600)")
        return None
    write_private(path, new_token(28) + "\n")
    report.add(f"created the admin password file {path} (random, mode 600)")
    return read_secret(path)


class UrlHttp:
    """The two plain HTTP calls Kuma's first-run screen needs."""

    def __init__(self, base: str, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _open(self, request: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise KumaError(f"cannot talk to Kuma at {self.base}: {exc}") from exc

    def get_json(self, path: str) -> Any:
        return self._open(urllib.request.Request(self.base + path))

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        return self._open(request)


# ---------------------------------------------------------------------------------------------------------------------
# the steps


def ensure_database(
    http: Http,
    apply: bool,
    report: Report,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Kuma 2's first screen. Returns True when the database still had to be chosen."""
    entry = http.get_json("/api/entry-page") or {}
    if entry.get("type") != "setup-database":
        return False
    if not apply:
        report.add("would choose SQLite on Kuma's first-run database screen")
        return True
    answer = http.post_json("/setup-database", {"dbConfig": {"type": "sqlite"}}) or {}
    if not answer.get("ok"):
        raise KumaError(f"Kuma refused the database choice: {answer}")
    deadline = clock() + DB_WAIT_SECONDS
    while (http.get_json("/api/entry-page") or {}).get("type") == "setup-database":
        if clock() > deadline:
            raise KumaError("Kuma did not finish its first start within three minutes")
        sleep(3)
    report.add("chose SQLite as Kuma's database")
    return True


def ensure_admin(kuma: Kuma, cfg: Config, password: str | None, report: Report) -> bool:
    """Make sure an admin exists and we are logged in. Returns False when (in plan mode) we cannot log in yet."""
    if kuma.call("needSetup"):
        if cfg.plan or password is None:
            report.add(f"would create the admin account '{cfg.user}'")
            return False
        answer = kuma.call("setup", cfg.user, password) or {}
        if not answer.get("ok"):
            raise KumaError(f"Kuma refused the admin account: {answer.get('msg')}")
        report.add(f"created the admin account '{cfg.user}'")
    if password is None:
        return False
    answer = kuma.call("login", {"username": cfg.user, "password": password, "token": ""}) or {}
    if not answer.get("ok"):
        raise KumaError(f"cannot log in as '{cfg.user}': {answer.get('msg')}")
    kuma.settle()
    return True


def telegram_payload(cfg: Config, token: str) -> dict[str, Any]:
    return {
        "name": cfg.notification_name,
        "type": "telegram",
        "telegramBotToken": token,
        "telegramChatID": cfg.telegram_chat_id,
        "telegramSendSilently": False,
        "telegramProtectContent": False,
        "isDefault": True,
        "applyExisting": True,
        "active": True,
    }


def ensure_notification(kuma: Kuma, cfg: Config, report: Report) -> None:
    if cfg.telegram_token_file is None or not cfg.telegram_chat_id:
        return
    if any(n.get("name") == cfg.notification_name for n in kuma.notifications()):
        report.add(f"notification '{cfg.notification_name}' already exists (left as it is)")
        return
    if cfg.plan:
        report.add(
            f"would add the Telegram notification '{cfg.notification_name}' (default, test message first)"
        )
        return
    token = read_secret(cfg.telegram_token_file)
    payload = telegram_payload(cfg, token)
    if cfg.test_notification:
        test = kuma.call("testNotification", payload) or {}
        if not test.get("ok"):
            raise KumaError(
                "the Telegram test message failed: "
                + str(test.get("msg")).replace(token, "<token>")
                + " (has the chat started a conversation with the bot?)"
            )
    answer = kuma.call("addNotification", payload, None) or {}
    if not answer.get("ok"):
        raise KumaError(
            "Kuma refused the notification: " + str(answer.get("msg")).replace(token, "<token>")
        )
    report.add(f"added the Telegram notification '{cfg.notification_name}' (default)")


def default_notification_ids(kuma: Kuma) -> dict[str, bool]:
    return {str(n["id"]): True for n in kuma.notifications() if n.get("isDefault") and "id" in n}


def push_monitor(
    name: str, token: str, interval: int, notification_ids: dict[str, bool]
) -> dict[str, Any]:
    return {
        "type": "push",
        "name": name,
        "pushToken": token,
        "interval": interval,
        "retryInterval": interval,
        "resendInterval": 0,
        "maxretries": 0,
        "notificationIDList": notification_ids,
        "accepted_statuscodes": ["200-299"],
        "conditions": [],
        "ignoreTls": False,
        "upsideDown": False,
        "expiryNotification": False,
        "maxredirects": 10,
        "dns_resolve_type": "A",
        "dns_resolve_server": "1.1.1.1",
        "method": "GET",
        "description": "Heartbeat; alerts when it stops (three missed beats).",
    }


def ensure_monitors(kuma: Kuma, cfg: Config, report: Report) -> dict[str, str]:
    """Create or correct the push monitors. Returns name -> push token (only for monitors that exist or were made)."""
    tokens: dict[str, str] = {}
    wanted_notifications = default_notification_ids(kuma)
    existing = {m.get("name"): m for m in kuma.monitors().values()}
    for name in cfg.monitors:
        found = existing.get(name)
        if found is None:
            if cfg.plan:
                report.add(f"would add the push monitor '{name}' (interval {cfg.interval} s)")
                continue
            token = new_token()
            answer = (
                kuma.call("add", push_monitor(name, token, cfg.interval, wanted_notifications))
                or {}
            )
            if not answer.get("ok"):
                raise KumaError(f"Kuma refused the monitor '{name}': {answer.get('msg')}")
            tokens[name] = token
            report.add(f"added the push monitor '{name}' (interval {cfg.interval} s)")
            continue
        if found.get("type") != "push":
            raise KumaError(f"'{name}' exists but is not a push monitor")
        tokens[name] = str(found.get("pushToken", ""))
        attached = {str(k) for k, v in (found.get("notificationIDList") or {}).items() if v}
        missing = set(wanted_notifications) - attached
        if found.get("interval") == cfg.interval and not missing:
            report.add(f"push monitor '{name}' is already right")
            continue
        if cfg.plan:
            report.add(f"would update the push monitor '{name}' (interval and notifications)")
            continue
        full = (kuma.call("getMonitor", found["id"]) or {}).get("monitor")
        if not full:
            raise KumaError(f"cannot read the monitor '{name}' back from Kuma")
        full["interval"] = cfg.interval
        full["retryInterval"] = cfg.interval
        full["notificationIDList"] = {
            **(full.get("notificationIDList") or {}),
            **wanted_notifications,
        }
        answer = kuma.call("editMonitor", full) or {}
        if not answer.get("ok"):
            raise KumaError(f"Kuma refused the update of '{name}': {answer.get('msg')}")
        report.add(f"updated the push monitor '{name}'")
    return tokens


def save_urls(cfg: Config, tokens: dict[str, str], report: Report) -> None:
    if cfg.urls_out is None or not tokens or cfg.plan:
        return
    lines: dict[str, str] = {}
    if cfg.urls_out.exists():
        for line in cfg.urls_out.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                lines[key] = value
    for name, token in tokens.items():
        if token:
            lines[env_key(name)] = f"{cfg.public_url.rstrip('/')}/api/push/{token}"
    write_private(cfg.urls_out, "".join(f"{k}={v}\n" for k, v in lines.items()))
    report.add(
        f"saved the push URLs to {cfg.urls_out} (mode 600): "
        + ", ".join(env_key(n) for n in tokens)
    )


def provision(kuma: Kuma, http: Http, cfg: Config) -> Report:
    report = Report()
    ensure_database(http, not cfg.plan, report)
    # in plan mode a missing password file is not created, so the admin step can only describe itself
    password = admin_password(cfg.password_file, cfg.plan, report)
    if not ensure_admin(kuma, cfg, password, report):
        return report
    ensure_notification(kuma, cfg, report)
    tokens = ensure_monitors(kuma, cfg, report)
    save_urls(cfg, tokens, report)
    return report


# ---------------------------------------------------------------------------------------------------------------------
# the real Socket.IO connection and the command line


class SocketIoClient:  # pragma: no cover - needs a live Kuma
    def __init__(self, url: str) -> None:
        try:
            import socketio
        except ImportError as exc:
            raise SystemExit(
                "python-socketio is not installed: run ops/monitor/kuma-provision.sh, or "
                "pip install 'python-socketio[client]' websocket-client"
            ) from exc
        self._monitors: dict[str, dict[str, Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self.sio = socketio.Client(reconnection=False)
        self.sio.on("monitorList", self._on_monitors)
        self.sio.on("notificationList", self._on_notifications)
        self.sio.connect(url, transports=["websocket"], wait_timeout=30)
        time.sleep(SETTLE_SECONDS)

    def _on_monitors(self, data: dict[str, dict[str, Any]]) -> None:
        self._monitors = data

    def _on_notifications(self, data: list[dict[str, Any]]) -> None:
        self._notifications = data

    def call(self, event: str, *args: Any) -> Any:
        payload = None if not args else (args[0] if len(args) == 1 else args)
        return self.sio.call(event, payload, timeout=40)

    def settle(self) -> None:
        time.sleep(SETTLE_SECONDS)

    def monitors(self) -> dict[str, dict[str, Any]]:
        return self._monitors

    def notifications(self) -> list[dict[str, Any]]:
        return self._notifications

    def close(self) -> None:
        self.sio.disconnect()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--url", required=True, help="where Kuma answers for this tool, e.g. http://127.0.0.1:3001"
    )
    p.add_argument(
        "--public-url", help="the address senders use, for the push URLs (default: --url)"
    )
    p.add_argument("--user", default="admin", help="admin user name (default: admin)")
    p.add_argument(
        "--admin-password-file",
        required=True,
        type=Path,
        help="read from here, or created (random, mode 600)",
    )
    p.add_argument(
        "--monitor",
        action="append",
        default=[],
        metavar="NAME",
        help="a push monitor to have (repeatable)",
    )
    p.add_argument(
        "--interval", type=int, default=900, help="seconds without a beat before Down (default 900)"
    )
    p.add_argument("--telegram-token-file", type=Path, help="file holding the Telegram bot token")
    p.add_argument(
        "--telegram-chat-id-file",
        type=Path,
        help="file holding the chat id that receives the alerts (best: a chat id on a command line shows in ps)",
    )
    p.add_argument(
        "--telegram-chat-id",
        default="",
        help="the chat that receives the alerts (prefer --telegram-chat-id-file)",
    )
    p.add_argument(
        "--notification-name", default="Telegram", help="name of the notification in Kuma"
    )
    p.add_argument(
        "--no-test-message", action="store_true", help="do not send a test message before adding it"
    )
    p.add_argument("--urls-out", type=Path, help="write NAME_URL=<push url> lines here (mode 600)")
    p.add_argument("--plan", action="store_true", help="say what would change and change nothing")
    return p


def main(
    argv: list[str] | None = None,
    kuma_factory: Callable[[str], Kuma] | None = None,
    http_factory: Callable[[str], Http] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.interval < 60:
        print("error: --interval must be at least 60 seconds", file=sys.stderr)
        return 1
    chat_id = args.telegram_chat_id
    if args.telegram_chat_id_file is not None:
        if chat_id:
            print(
                "error: give --telegram-chat-id-file or --telegram-chat-id, not both",
                file=sys.stderr,
            )
            return 1
        try:
            chat_id = read_secret(args.telegram_chat_id_file)
        except (OSError, KumaError) as exc:
            print(f"error: cannot use the chat id file: {exc}", file=sys.stderr)
            return 1
    cfg = Config(
        url=args.url.rstrip("/"),
        public_url=(args.public_url or args.url).rstrip("/"),
        user=args.user,
        password_file=args.admin_password_file,
        monitors=args.monitor,
        interval=args.interval,
        telegram_token_file=args.telegram_token_file,
        telegram_chat_id=chat_id,
        notification_name=args.notification_name,
        urls_out=args.urls_out,
        plan=args.plan,
        test_notification=not args.no_test_message,
    )
    http = (http_factory or UrlHttp)(cfg.url)
    kuma: Kuma | None = None
    try:
        # the database screen is plain HTTP; the Socket.IO channel only works after it, so do it before connecting
        early = Report()
        if ensure_database(http, not cfg.plan, early) and cfg.plan:
            for line in early.lines:
                print("plan:", line)
            print("plan: nothing else can be checked until the database is chosen")
            return 0
        kuma = (kuma_factory or SocketIoClient)(cfg.url)
        report = provision(kuma, http, cfg)
        for line in early.lines + report.lines:
            print(("plan: " if cfg.plan else "ok: ") + line)
        if not report.lines and not early.lines:
            print("ok: nothing to do")
        return 0
    except KumaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if kuma is not None:
            kuma.close()


if __name__ == "__main__":
    sys.exit(main())
