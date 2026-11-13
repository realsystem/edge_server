#!/usr/bin/env python3
"""Telegram bot for the edge server (standard library only).

Commands, for the users listed in TELEGRAM_ALLOWED_IDS:
  /status  quick health check: load, memory, disk, temperature, power, Frigate
  /diag    full diagnostics report (collect-diagnostics.sh), sent as a file
  /testalert [percent]  send a simulated low-disk alert to you (to test delivery)
  /help    list the commands
With the authenticator set up (REBOOT_TOTP_SECRET) there are also:
  /reboot <code>, /redeploy <code>, /restart <service> <code>
  /logs <service> [lines]   recent log lines of one container (passwords removed)

It also sends a message to every authorized user when the recordings or system
disk drops below ALERT_DISK_FREE_PERCENT free (default 15, 0 turns it off),
checked every ALERT_INTERVAL seconds (300), repeated every ALERT_REPEAT_HOURS
(6) while the disk stays low, and again when it has recovered.
Anyone can use /id to learn their own Telegram user id (needed for setup).
Messages from anybody else are ignored.

The bot polls Telegram (long polling), so the server only makes outbound HTTPS
requests to api.telegram.org: no port is opened.

Environment:
  TELEGRAM_BOT_TOKEN    from @BotFather (required)
  TELEGRAM_ALLOWED_IDS  comma-separated Telegram user ids
  FRIGATE_URL           default http://frigate:5000
  EDGE_SERVER_DIR       default /opt/edge-server (the stack directory)
  STORAGE_PATH          default /media/frigate (recordings, for the disk check)
  STORAGE_LABEL         host path shown in /status for that disk, default /mnt/storage
  HA_HOST               address of a Home Assistant on the LAN: /status shows two
                        lines, ping and HTTP on HA_PORT. Empty (the default here) = off.
                        The deploy script sets it for you (bot-defaults.env); a
                        value in .telegram wins.
  HA_PORT               default 8123
  MODELS_CONFIG         model routing file for /ask (autonomy ops/models.example.yaml);
                        without it /ask uses llm.chat on the job processor (PROCESSOR_URL)
  LLM_MODEL             model for that default route (empty: the worker's default)
  MODEL_REVIEW          true: an LLM reviews governed actions too (escalate-only)
  PROCESSOR_URL         the job processor on this node (INSTALL_PROCESSOR=true): /status
                        shows its queue and workers, and the watcher alerts when jobs
                        wait PROCESSOR_ALERT_MINUTES (default 30) with no worker online
  PROCESSOR_API_KEY     API key for it, or PROCESSOR_API_KEY_FILE: a file holding it,
                        read at each check (the deploy script creates one there)
  EDGE_NODE             this node's name from the fleet file, shown in /status
  EDGE_OPERATORS        user ids from the fleet file, used when TELEGRAM_ALLOWED_IDS is empty
  ALERT_DISK_FREE_PERCENT, ALERT_INTERVAL, ALERT_REPEAT_HOURS  see above
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("edge-bot")

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
TELEGRAM_TEXT_LIMIT = 3500  # the API allows 4096; leave room for <pre> markup
COMMANDS = [
    {"command": "status", "description": "Quick health check"},
    {"command": "diag", "description": "Full diagnostics report (file)"},
    {"command": "testalert", "description": "Send a test low-disk alert"},
    {"command": "id", "description": "Show your Telegram user id"},
    {"command": "help", "description": "List commands"},
]

HELP = (
    "Edge server bot\n"
    "/status - quick health check\n"
    "/diag - full diagnostics report (a file, takes about 15 s)\n"
    "/testalert [percent] - simulate a low-disk alert, sent only to you\n"
    "/id - your Telegram user id"
)
FLEET_COMMAND = {
    "command": "fleet",
    "description": "The other nodes: contact, versions, fleet file",
}
AUDIT_HELP = (
    "\n/audit - recent governed actions (signed, hash-chained audit log)"
    "\n/pending, /approve <id> <code>, /deny <id> - actions waiting for your approval"
    "\n/suspend <all|cell|component> [reason], /hold [reason] - stop automatic actions; /resume [scope] <code>"
)
FARM_COMMAND = {"command": "farm", "description": "Field Station: latest sensor readings"}
FARM_HELP = "\n/farm - the Field Station's latest readings (read only)"
ASK_COMMAND = {"command": "ask", "description": "Ask the assistant about this node"}
ASK_HELP = "\n/ask <question> - ask the assistant about this node (a model on your GPU machine)"
FLEET_HELP = "\n/fleet - the other nodes of the fleet: last contact, versions, fleet file"
REBOOT_COMMAND = {"command": "reboot", "description": "Reboot the laptop (authenticator code)"}
REBOOT_HELP = "\n/reboot <code> - reboot the laptop; code from your authenticator app"
REDEPLOY_COMMAND = {
    "command": "redeploy",
    "description": "Deploy the newest release and redeploy (authenticator code)",
}
REDEPLOY_HELP = (
    "\n/redeploy <code> - deploy the newest release tag (vX.Y) from GitHub; same authenticator code"
)
REDEPLOY_RESULT_MAX_AGE = 30 * 60.0
LOGS_INLINE_LIMIT = 3500  # longer logs are sent as a file
RESTART_COMMAND = {
    "command": "restart",
    "description": "Restart one container (authenticator code)",
}
RESTART_HELP = (
    "\n/restart <service> <code> - restart one container, e.g. frigate; same authenticator code"
)
LOGS_COMMAND = {"command": "logs", "description": "Recent log lines of one container"}
LOGS_HELP = "\n/logs <service> [lines] - last log lines of a container (default 100, at most 1000)"
HIDDEN_FROM_RESTART = {
    "telegram-bot"
}  # restarting the bot from the bot would cut off its own answer

TOTP_STEP = 30
TOTP_FILE_AGE = 60  # a /reboot message older than this is refused
REBOOT_MAX_FAILURES = 3
REBOOT_LOCKOUT = 15 * 60.0
REBOOT_NOTIFY_MAX_AGE = 30 * 60.0
VERIFY_INTERVAL = 30.0  # how often executed actions are checked against their effect (M8)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class Config:
    token: str
    allowed: frozenset[int] = frozenset()
    frigate_url: str = "http://frigate:5000"
    app_dir: Path = Path("/opt/edge-server")
    storage_path: Path = Path("/media/frigate")
    storage_label: str = "/mnt/storage"  # where the host mounts it; only shown in /status
    ha_host: str = ""  # Home Assistant on the LAN checked in /status; "" = off
    node: str = ""  # this node's name in the fleet file, shown in /status
    ha_port: int = 8123
    processor_url: str = ""  # job processor control plane (INSTALL_PROCESSOR); "" = off
    processor_api_key: str = ""
    processor_api_key_file: Path | None = None
    processor_alert_minutes: float = 30.0
    models_config: Path | None = None  # MODELS_CONFIG: routing for /ask (M7)
    llm_model: str = ""
    model_review: bool = False
    ha_timeout: float = 3.0
    proc: Path = Path("/proc")
    sys: Path = Path("/sys")
    api_url: str = "https://api.telegram.org"
    diag_script: Path = Path("/app/collect-diagnostics.sh")
    diag_samples: int = 2
    diag_interval: int = 3
    diag_cooldown: float = 30.0
    max_age: int = 300  # ignore commands older than this (sent while the bot was down)
    alert_free_percent: float = 15.0  # alert below this much free disk; 0 = off
    alert_interval: float = 300.0  # seconds between disk checks
    alert_repeat: float = 6 * 3600.0  # seconds between reminders while the disk stays low
    watch: bool = True  # alert on Frigate down, camera offline, hot CPU, failing disk
    watch_interval: float = 60.0  # seconds between watcher checks
    alert_confirm: int = 3  # checks in a row before alerting (ignores restarts and blips)
    alert_temp_c: float = 85.0  # CPU temperature alert level; 0 = off
    alert_wear_percent: float = 80.0  # alert when an SSD reports this much of its life used
    status_dir: Path = Path("/status")  # disk health written by the host (smart-report.py)
    # fleet control plane (controlplane.py): on only when the host wrote status_dir/fleet.json
    control_key: Path = Path("/run/edge/node.key")
    control_state: Path = Path("/state")
    farm_status: Path | None = None  # Field Station cell's status.json; /farm is off without it
    farm_stale: float = 120.0  # seconds without a status update before "not reporting"
    farm_alert_station: bool = True  # alert when the cell stops reporting or loses Home Assistant
    farm_alert_confirm: int = 0  # checks in a row before a farm alert; 0 = alert_confirm
    farm_alerts: str = (
        ""  # threshold rules, e.g. "sensor.wh51_ch1_soil_moisture<20,sensor.ws90_temperature>40"
    )
    control_bind: str = "0.0.0.0"  # noqa: S104  # the host publishes the port on LAN/Tailscale only
    smart_max_age: float = 3 * 86400.0  # alert if that file is older than this
    totp_secret: str = ""  # base32; empty = /reboot is off
    requests_dir: Path = Path("/requests")  # shared with the host-side reboot watcher
    reboot_min_uptime: float = 600.0  # refuse to reboot a host that booted less than this ago
    redeploy_poll: float = 5.0  # seconds between looks for the redeploy result
    redeploy_start_wait: float = 120.0  # the host must pick the request up within this
    redeploy_max_wait: float = 2700.0  # give up waiting for a result after this
    job_poll: float = 2.0  # seconds between looks for a /restart or /logs answer from the host
    job_start_wait: float = 30.0  # the host must pick the request up within this
    restart_max_wait: float = 300.0  # give up waiting for a restart result after this
    logs_max_wait: float = 90.0
    logs_cooldown: float = 10.0
    reboot_wait: float = 150.0  # seconds to wait for the host to go down before reporting failure

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not TOKEN_RE.match(token):
            raise ValueError("TELEGRAM_BOT_TOKEN is missing or not a bot token (123456:ABC...)")
        allowed: set[int] = set()
        # the fleet file's operators are the fallback when .telegram lists nobody
        ids = env.get("TELEGRAM_ALLOWED_IDS", "").strip() or env.get("EDGE_OPERATORS", "").strip()
        for item in re.split(r"[,\s]+", ids):
            if not item:
                continue
            if not item.isdigit():
                raise ValueError(f"TELEGRAM_ALLOWED_IDS: {item!r} is not a numeric user id")
            allowed.add(int(item))
        try:
            percent = float(env.get("ALERT_DISK_FREE_PERCENT", cls.alert_free_percent))
        except ValueError:
            raise ValueError("ALERT_DISK_FREE_PERCENT must be a number") from None
        if not 0 <= percent <= 100:
            raise ValueError("ALERT_DISK_FREE_PERCENT must be between 0 and 100")
        totp_secret = normalize_secret(env.get("REBOOT_TOTP_SECRET", ""))
        try:
            temp = float(env.get("ALERT_TEMP_C", cls.alert_temp_c))
            wear = float(env.get("ALERT_DISK_WEAR_PERCENT", cls.alert_wear_percent))
            confirm = int(env.get("ALERT_CONFIRM", cls.alert_confirm))
            watch_interval = float(env.get("ALERT_WATCH_INTERVAL", cls.watch_interval))
        except ValueError:
            raise ValueError(
                "ALERT_TEMP_C, ALERT_DISK_WEAR_PERCENT, ALERT_CONFIRM and "
                "ALERT_WATCH_INTERVAL must be numbers"
            ) from None
        ha_host = env.get("HA_HOST", cls.ha_host).strip()
        if ha_host and not HOST_RE.match(ha_host):
            raise ValueError("HA_HOST must be an IP address or host name")
        try:
            ha_port = int(env.get("HA_PORT", cls.ha_port))
        except ValueError:
            raise ValueError("HA_PORT must be a number") from None
        if not 1 <= ha_port <= 65535:
            raise ValueError("HA_PORT must be between 1 and 65535")
        watch = env.get("ALERT_WATCH", "true").strip().lower() not in ("false", "0", "no", "off")
        try:
            processor_alert = float(env.get("PROCESSOR_ALERT_MINUTES", cls.processor_alert_minutes))
        except ValueError:
            raise ValueError("PROCESSOR_ALERT_MINUTES must be a number") from None
        return cls(
            token=token,
            allowed=frozenset(allowed),
            frigate_url=env.get("FRIGATE_URL", cls.frigate_url).rstrip("/"),
            app_dir=Path(env.get("EDGE_SERVER_DIR", str(cls.app_dir))),
            storage_path=Path(env.get("STORAGE_PATH", str(cls.storage_path))),
            storage_label=env.get("STORAGE_LABEL", cls.storage_label).strip() or cls.storage_label,
            ha_host=ha_host,
            ha_port=ha_port,
            processor_url=env.get("PROCESSOR_URL", "").strip().rstrip("/"),
            processor_api_key=env.get("PROCESSOR_API_KEY", "").strip(),
            processor_api_key_file=(
                Path(env["PROCESSOR_API_KEY_FILE"]) if env.get("PROCESSOR_API_KEY_FILE") else None
            ),
            processor_alert_minutes=processor_alert,
            models_config=Path(env["MODELS_CONFIG"]) if env.get("MODELS_CONFIG") else None,
            llm_model=env.get("LLM_MODEL", "").strip(),
            model_review=env.get("MODEL_REVIEW", "").strip().lower() in ("1", "true", "yes", "on"),
            node=env.get("EDGE_NODE", "").strip(),
            proc=Path(env.get("HOST_PROC", str(cls.proc))),
            sys=Path(env.get("HOST_SYS", str(cls.sys))),
            api_url=env.get("TELEGRAM_API_URL", cls.api_url).rstrip("/"),
            diag_script=Path(env.get("DIAG_SCRIPT", str(cls.diag_script))),
            diag_samples=int(env.get("DIAG_SAMPLES", cls.diag_samples)),
            diag_interval=int(env.get("DIAG_INTERVAL", cls.diag_interval)),
            diag_cooldown=float(env.get("DIAG_COOLDOWN", cls.diag_cooldown)),
            alert_free_percent=percent,
            alert_interval=float(env.get("ALERT_INTERVAL", cls.alert_interval)),
            alert_repeat=float(env.get("ALERT_REPEAT_HOURS", cls.alert_repeat / 3600)) * 3600,
            watch=watch,
            alert_confirm=max(1, confirm),
            watch_interval=max(5.0, watch_interval),
            alert_temp_c=temp,
            alert_wear_percent=wear,
            status_dir=Path(env.get("STATUS_DIR", str(cls.status_dir))),
            control_key=Path(env.get("CONTROL_KEY", str(cls.control_key))),
            control_state=Path(env.get("CONTROL_STATE", str(cls.control_state))),
            farm_status=Path(env["FARM_STATUS_FILE"]) if env.get("FARM_STATUS_FILE") else None,
            farm_stale=max(10.0, float(env.get("FARM_STALE_SECONDS", cls.farm_stale))),
            farm_alert_station=env.get("FARM_ALERT_STATION", "true").lower() != "false",
            farm_alert_confirm=max(0, int(env.get("FARM_ALERT_CONFIRM", cls.farm_alert_confirm))),
            farm_alerts=env.get("FARM_ALERTS", ""),
            control_bind=env.get("CONTROL_BIND", cls.control_bind),
            totp_secret=totp_secret,
            requests_dir=Path(env.get("REQUESTS_DIR", str(cls.requests_dir))),
        )


# ---------------------------------------------------------------------------
# TOTP (RFC 6238: HMAC-SHA1, 30 s steps, 6 digits - what authenticator apps use)
# ---------------------------------------------------------------------------


def normalize_secret(raw: str) -> str:
    """Clean up a base32 secret as typed by a person; "" means not configured."""
    secret = re.sub(r"[\s-]+", "", raw).upper().rstrip("=")
    if not secret:
        return ""
    if not re.fullmatch(r"[A-Z2-7]{16,}", secret):
        raise ValueError("REBOOT_TOTP_SECRET must be base32 (A-Z, 2-7), at least 16 characters")
    try:
        base64.b32decode(secret + "=" * (-len(secret) % 8))
    except ValueError:
        raise ValueError("REBOOT_TOTP_SECRET is not valid base32") from None
    return secret


def totp(secret: str, step: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 10**digits:0{digits}d}"


def verify_totp(
    secret: str, code: str, now: float, last_step: int = 0, window: int = 1
) -> int | None:
    """Return the time step the code belongs to, or None.

    Steps at or before last_step are refused, so a code works once even if it is
    seen again (replay). window allows for clock drift of +-window steps.
    """
    base = int(now // TOTP_STEP)
    accepted = None
    for step in range(base - window, base + window + 1):
        # no early exit: every candidate is compared in constant time
        if hmac.compare_digest(totp(secret, step), code) and step > last_step:
            accepted = step
    return accepted


def totp_setup(
    out: Callable[[str], None] = print, rand: Callable[[int], bytes] = os.urandom
) -> int:
    secret = base64.b32encode(rand(20)).decode()
    uri = f"otpauth://totp/Edge%20server:reboot?secret={secret}&issuer=Edge%20server"
    out("Add this to your authenticator app (Google Authenticator, Aegis, 2FAS, ...):")
    out(f"  secret: {secret}")
    out(f"  or open this link / make a QR code from it: {uri}")
    out("")
    out("Then put this line in .telegram on the server and redeploy:")
    out(f"  REBOOT_TOTP_SECRET={secret}")
    out("")
    out(
        f"The app should now show the code {totp(secret, int(time.time() // TOTP_STEP))} (it changes every 30 s)."
    )
    return 0


# ---------------------------------------------------------------------------
# Telegram Bot API
# ---------------------------------------------------------------------------


class TelegramError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class Telegram:
    """Minimal Bot API client. Errors never contain the token (it is in the URL)."""

    def __init__(self, token: str, base: str = "https://api.telegram.org"):
        self._base = f"{base}/bot{token}/"

    def call(self, method: str, payload: dict | None = None, timeout: float = 15) -> object:
        request = urllib.request.Request(
            self._base + method,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        return self._open(request, method, timeout)

    def send_document(
        self, chat_id: int, filename: str, data: bytes, caption: str = "", timeout: float = 60
    ) -> object:
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []

        def field_(name: str, value: str) -> None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )

        field_("chat_id", str(chat_id))
        if caption:
            field_("caption", caption[:1000])
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                f'filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'
            ).encode()
            + data
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            self._base + "sendDocument",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return self._open(request, "sendDocument", timeout)

    @staticmethod
    def _open(request: urllib.request.Request, method: str, timeout: float) -> object:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = str(json.loads(e.read()).get("description", ""))
            except (ValueError, OSError, AttributeError):
                pass
            raise TelegramError(f"{method}: HTTP {e.code} {detail}".strip(), e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise TelegramError(f"{method}: {getattr(e, 'reason', e)}") from None
        try:
            data = json.loads(body)
        except ValueError:
            raise TelegramError(f"{method}: invalid response") from None
        if not data.get("ok"):
            raise TelegramError(
                f"{method}: {data.get('description', 'not ok')}", data.get("error_code")
            )
        return data.get("result")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def gib(n: float) -> str:
    return f"{n / 2**30:.1f}"


def cpu_times(proc: Path) -> tuple[float, float] | None:
    """(busy, total) jiffies since boot from the aggregate ``cpu`` line."""
    text = read_text(proc / "stat")
    if not text:
        return None
    fields = text.splitlines()[0].split()[1:]
    try:
        values = [float(x) for x in fields]
    except ValueError:
        return None
    total = sum(values[:8])
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    return total - idle, total


def meminfo(proc: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in (read_text(proc / "meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        if rest.split():
            result[key] = int(rest.split()[0]) * 1024
    return result


def temperatures(sys_root: Path) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    for zone in sorted((sys_root / "class/thermal").glob("thermal_zone*")):
        raw, name = read_text(zone / "temp"), read_text(zone / "type") or zone.name
        if raw and raw.lstrip("-").isdigit():
            found.append((name, int(raw) / 1000))
    if not found:
        for hwmon in sorted((sys_root / "class/hwmon").glob("hwmon*")):
            name = read_text(hwmon / "name") or hwmon.name
            for temp in sorted(hwmon.glob("temp*_input")):
                raw = read_text(temp)
                if raw and raw.lstrip("-").isdigit():
                    found.append((name, int(raw) / 1000))
    return found


def power_state(sys_root: Path) -> tuple[str, bool]:
    """(description, on_battery)."""
    mains: list[bool] = []
    batteries: list[str] = []
    discharging = False
    for supply in sorted((sys_root / "class/power_supply").glob("*")):
        kind = read_text(supply / "type")
        if kind == "Mains":
            mains.append(read_text(supply / "online") == "1")
        elif kind == "Battery":
            capacity = read_text(supply / "capacity") or "?"
            status = read_text(supply / "status") or "?"
            batteries.append(f"battery {capacity}% {status.lower()}")
            discharging = discharging or status == "Discharging"
    if not mains and not batteries:
        return "power n/a", False
    on_battery = discharging or (bool(mains) and not any(mains))
    head = "ON BATTERY" if on_battery else "AC power"
    return ", ".join([head, *batteries]), on_battery


def disk_line(label: str, path: Path) -> tuple[str, bool]:
    try:
        st = os.statvfs(path)
    except OSError:
        return f"{label}: n/a", False
    total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    pct_free = 100 * free / total if total else 0
    return f"{label}: {gib(free)} of {gib(total)} GB free ({pct_free:.0f}%)", pct_free < 10


HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def icmp_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return int(~total & 0xFFFF)


def ping_host(host: str, count: int = 3, timeout: float = 1.0) -> tuple[int, int, float] | None:
    """Echo requests over an unprivileged ICMP socket (no ping binary, no capability).
    Returns (replies, sent, average ms), or None if this container may not send ICMP.
    Raises OSError if the name does not resolve."""
    address = socket.gethostbyname(host)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except OSError:
        return None  # net.ipv4.ping_group_range does not cover this process
    sock.settimeout(timeout)
    rtts: list[float] = []
    try:
        for seq in range(1, count + 1):
            header = struct.pack("!BBHHH", 8, 0, 0, 0, seq)
            payload = b"edge-server-status"
            checksum = icmp_checksum(header + payload)
            packet = struct.pack("!BBHHH", 8, 0, checksum, 0, seq) + payload
            started = time.monotonic()
            try:
                sock.sendto(packet, (address, 0))
            except PermissionError:
                return None  # sending is refused (ping_group_range, a firewall)
            except OSError:
                continue  # network unreachable: counts as lost
            deadline = started + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    data, _ = sock.recvfrom(512)
                except OSError:  # timeout
                    break
                # an ICMP datagram socket delivers the ICMP message without the IP header
                if len(data) >= 8 and data[0] == 0 and struct.unpack("!H", data[6:8])[0] == seq:
                    rtts.append((time.monotonic() - started) * 1000)
                    break
    finally:
        sock.close()
    return len(rtts), count, (sum(rtts) / len(rtts) if rtts else 0.0)


def http_probe(host: str, port: int, timeout: float) -> tuple[int | None, float, str]:
    """GET / on host:port. (status code or None, milliseconds, short error)."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/", timeout=timeout
        ) as response:  # noqa: S310
            return response.status, (time.monotonic() - started) * 1000, ""
    except urllib.error.HTTPError as e:  # it answered, e.g. 401 or 503
        return e.code, (time.monotonic() - started) * 1000, ""
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ConnectionRefusedError):
            return None, (time.monotonic() - started) * 1000, "connection refused"
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return None, (time.monotonic() - started) * 1000, "timed out"
        return None, (time.monotonic() - started) * 1000, str(reason)[:60]
    except (TimeoutError, OSError) as e:
        text = "timed out" if isinstance(e, TimeoutError) else str(e)[:60]
        return None, (time.monotonic() - started) * 1000, text


def home_assistant_lines(cfg: Config) -> tuple[list[str], list[str]]:
    """Two compact lines for /status (ping, HTTP) and the problems they imply."""
    host, port = cfg.ha_host, cfg.ha_port
    with ThreadPoolExecutor(max_workers=2) as pool:
        ping_job = pool.submit(ping_host, host, 3, 1.0)
        http_job = pool.submit(http_probe, host, port, cfg.ha_timeout)
        try:
            ping = ping_job.result()
            ping_error = ""
        except OSError:
            ping, ping_error = None, "cannot resolve the name"
        code, ms, error = http_job.result()

    problems: list[str] = []
    label = f"home assistant {host}"
    if ping_error:
        ping_line = f"{label} ping: {ping_error}"
        problems.append(f"Home Assistant {host}: {ping_error}")
    elif ping is None:
        ping_line = f"{label} ping: n/a (this container may not send ICMP)"
    else:
        replies, sent, avg = ping
        if replies:
            ping_line = f"{label} ping: {replies}/{sent} replies, avg {avg:.0f} ms"
            if replies < sent:
                problems.append(f"Home Assistant {host}: lost {sent - replies} of {sent} pings")
        else:
            ping_line = f"{label} ping: no reply (0/{sent})"
            problems.append(f"Home Assistant {host} does not answer ping")

    if code is None:
        http_line = f"{label}:{port} HTTP: {error or 'no answer'} ({ms:.0f} ms)"
        problems.append(f"Home Assistant web interface ({host}:{port}): {error or 'no answer'}")
    else:
        http_line = f"{label}:{port} HTTP {code} in {ms:.0f} ms"
        if code >= 500:
            problems.append(f"Home Assistant web interface answers HTTP {code}")
    return [ping_line, http_line], problems


def storage_usage_line(label: str, path: Path) -> str:
    """How full the storage disk is, as used space (disk_line reports free space)."""
    try:
        st = os.statvfs(path)
    except OSError:
        return f"storage {label}: n/a"
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    pct = 100 * used / total if total else 0
    return f"storage {label}: {gib(used)} of {gib(total)} GB used ({pct:.0f}%)"


RECOVERY_MARGIN = 3.0  # percentage points above the alert level before "recovered"


def disk_usage(path: Path) -> tuple[int, int] | None:
    """(total, free) bytes for the filesystem holding ``path``."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def system_path(cfg: Config) -> Path:
    """A path on the host's system disk. app_dir itself is only the container's own
    filesystem; its frigate/ subfolder is a bind mount of the host's."""
    frigate = cfg.app_dir / "frigate"
    return frigate if frigate.exists() else cfg.app_dir


def watched_disks(cfg: Config) -> list[tuple[str, Path]]:
    """Disks to watch, one entry per filesystem (recordings often share the system disk)."""
    groups: dict[int, tuple[list[str], Path]] = {}
    for label, path in (("recordings", cfg.storage_path), ("system disk", system_path(cfg))):
        try:
            device = os.stat(path).st_dev
        except OSError:
            continue
        labels, first = groups.setdefault(device, ([], path))
        labels.append(label)
    return [(" and ".join(labels), path) for labels, path in groups.values()]


def format_disk_alert(
    label: str,
    free: int,
    total: int,
    threshold: float,
    reminder: bool = False,
    test: bool = False,
) -> str:
    pct = 100 * free / total if total else 0
    head = "⚠️ Low disk space" + (" (reminder)" if reminder else "")
    lines = [
        *(["TEST - this is not a real alert", ""] if test else []),
        head,
        f"{label}: {gib(free)} of {gib(total)} GB free ({pct:.1f}%), alert level {threshold:g}%.",
        "Frigate starts deleting the oldest recordings when about an hour of space is "
        "left, whatever the retention settings.",
        "Shorten the recording retention, lower the cameras' bitrate, or add storage.",
    ]
    return "\n".join(lines)


def http_get(url: str, timeout: float = 5) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - internal URL
        body: bytes = response.read()
        return body


def processor_stats(cfg: Config) -> tuple[dict | None, str]:
    """The job processor's /api/v1/stats, or (None, why not)."""
    key = cfg.processor_api_key
    if not key and cfg.processor_api_key_file is not None:
        key = (read_text(cfg.processor_api_key_file) or "").strip()
    if not key:
        return None, "no API key yet (PROCESSOR_API_KEY or PROCESSOR_API_KEY_FILE)"
    request = urllib.request.Request(
        f"{cfg.processor_url}/api/v1/stats", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - internal URL
            body = json.loads(response.read())
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}" + (" (check PROCESSOR_API_KEY)" if e.code == 401 else "")
    except (OSError, ValueError) as e:
        return None, f"no answer ({getattr(e, 'reason', e)})"
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None, "unexpected answer"
    return data, ""


def processor_problem(cfg: Config, stats: dict | None, error: str) -> str | None:
    """What is wrong with the job processor, if anything: unreachable, or jobs waiting
    longer than PROCESSOR_ALERT_MINUTES with no worker online."""
    if stats is None:
        return f"Job processor at {cfg.processor_url}: {error}."
    queued = int((stats.get("jobs") or {}).get("queued", 0))
    waited = float(stats.get("oldest_queued_s") or 0)
    if queued and not stats.get("workers_online") and waited >= cfg.processor_alert_minutes * 60:
        return (
            f"{queued} job(s) have waited {human_duration(waited)} and no processor worker is "
            "online. Is the GPU machine on and connected (Tailscale)?"
        )
    return None


def processor_lines(cfg: Config) -> tuple[list[str], list[str]]:
    """Lines for /status: queue, failures, online workers; and the problems they imply."""
    stats, error = processor_stats(cfg)
    problem = processor_problem(cfg, stats, error)
    if stats is None:
        return [f"processor: {error}"], [problem] if problem else []
    jobs = stats.get("jobs") or {}
    queued, running = int(jobs.get("queued", 0)), int(jobs.get("running", 0)) + int(
        jobs.get("leased", 0)
    )
    failed = int(jobs.get("failed", 0)) + int(jobs.get("dead_letter", 0))
    queue = f"processor: {queued} queued"
    if queued:
        queue += f" (oldest {human_duration(float(stats.get('oldest_queued_s') or 0))})"
    queue += f", {running} running, {int(jobs.get('succeeded', 0))} done, {failed} failed"
    workers = stats.get("workers_online") or []
    if workers:
        names = ", ".join(
            f"{w.get('id')} ({', '.join(w.get('capabilities') or []) or 'no capabilities'})"
            for w in workers
        )
        worker_line = f"processor workers online: {names}"
    else:
        worker_line = "processor workers online: none"
    return [queue, worker_line], [problem] if problem else []


def disabled_cameras(cfg: Config) -> set[str]:
    """Cameras switched off in the Frigate config (they report 0 fps by design)."""
    try:
        config = json.loads(http_get(f"{cfg.frigate_url}/api/config"))
    except (OSError, ValueError, urllib.error.URLError):
        return set()
    cameras = config.get("cameras") if isinstance(config, dict) else None
    return {
        name
        for name, camera in (cameras or {}).items()
        if isinstance(camera, dict) and camera.get("enabled") is False
    }


def frigate_status(cfg: Config) -> tuple[list[str], list[str]]:
    """Lines about Frigate and a list of problems."""
    try:
        version = http_get(f"{cfg.frigate_url}/api/version").decode().strip()
        stats = json.loads(http_get(f"{cfg.frigate_url}/api/stats"))
    except (OSError, ValueError, urllib.error.URLError) as e:
        reason = getattr(e, "reason", e)
        return [f"Frigate: NOT REACHABLE ({reason})"], ["Frigate is not reachable"]

    problems: list[str] = []
    uptime = (stats.get("service") or {}).get("uptime")
    lines = [f"Frigate {version}" + (f", up {human_duration(uptime)}" if uptime else "")]
    for name, d in (stats.get("detectors") or {}).items():
        speed = d.get("inference_speed")
        lines.append(
            f"  detector {name}: {speed} ms" if speed is not None else f"  detector {name}"
        )
    disabled = disabled_cameras(cfg)
    for name, c in sorted((stats.get("cameras") or {}).items()):
        if name in disabled:
            lines.append(f"  {name}: disabled")
            continue
        fps = float(c.get("camera_fps") or 0)
        detect = float(c.get("detection_fps") or 0)
        skipped = float(c.get("skipped_fps") or 0)
        if fps < 0.5:
            lines.append(f"  {name}: NO VIDEO")
            problems.append(f"{name}: no video")
            continue
        extra = f", {skipped:.1f} skipped/s" if skipped >= 0.5 else ""
        lines.append(f"  {name}: {fps:.0f} fps, detecting {detect:.1f}/s{extra}")
        if skipped >= 0.5:
            problems.append(f"{name}: detector too slow, dropping frames")
    if not stats.get("cameras"):
        lines.append("  no cameras configured")
    return lines, problems


def build_status(cfg: Config, sleep: Callable[[float], None] = time.sleep) -> str:
    problems: list[str] = []
    host: list[str] = []

    uptime = (read_text(cfg.proc / "uptime") or "0").split()[0]
    cores = os.cpu_count() or 1
    load = (read_text(cfg.proc / "loadavg") or "0 0 0").split()[:3]
    first = cpu_times(cfg.proc)
    sleep(1.0)
    second = cpu_times(cfg.proc)
    busy = ""
    if first and second and second[1] > first[1]:
        busy = f", CPU {100 * (second[0] - first[0]) / (second[1] - first[1]):.0f}%"
    host.append(
        f"up {human_duration(float(uptime))}, load {' / '.join(load)} ({cores} cores){busy}"
    )
    try:
        if float(load[1]) / cores > 1.5:
            problems.append(f"high load ({load[1]} on {cores} cores)")
    except ValueError:
        pass

    mem = meminfo(cfg.proc)
    if mem.get("MemTotal"):
        used = mem["MemTotal"] - mem.get("MemAvailable", 0)
        swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
        host.append(
            f"memory {gib(used)} of {gib(mem['MemTotal'])} GB used, swap {gib(swap_used)} GB used"
        )
        if mem.get("MemAvailable", 1) / mem["MemTotal"] < 0.05:
            problems.append("memory almost full")

    temps = temperatures(cfg.sys)
    power, on_battery = power_state(cfg.sys)
    if temps:
        name, hottest = max(temps, key=lambda t: t[1])
        host.append(f"temperature {hottest:.0f} C ({name}), {power}")
        if hottest >= 90:
            problems.append(f"very hot ({hottest:.0f} C)")
    else:
        host.append(power)
    if on_battery:
        problems.append("running on battery")

    for label, path in (("recordings", cfg.storage_path), ("system disk", system_path(cfg))):
        line, low = disk_line(label, path)
        host.append(line)
        if low:
            problems.append(f"{label} almost full")
        if label == "recordings":
            host.append(storage_usage_line(cfg.storage_label, path))

    if cfg.ha_host:
        ha_lines, ha_problems = home_assistant_lines(cfg)
        host += ha_lines
        problems += ha_problems

    if cfg.processor_url:
        processor, processor_problems = processor_lines(cfg)
        host += processor
        problems += processor_problems

    frigate, frigate_problems = frigate_status(cfg)
    problems += frigate_problems

    head = "OK" if not problems else f"{len(problems)} problem(s)"
    who = f" ({cfg.node})" if cfg.node else ""
    out = [f"{'✅' if not problems else '⚠️'} Edge server{who}: {head}"]
    out += [f"  - {p}" for p in problems]
    out += ["", "Host", *[f"  {line}" for line in host], "", *frigate]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


@dataclass
class DiskState:
    low: bool = False
    last_alert: float = 0.0


@dataclass
class Incident:
    """One thing being watched that is currently wrong."""

    label: str
    strikes: int = 0
    alerted: bool = False
    last_alert: float = 0.0


def no_video_message(names: list[str], reminder: bool = False) -> str:
    """One message for all cameras that went dark together (power cut, switch down)."""
    tail = " (reminder)" if reminder else ""
    if len(names) == 1:
        return (
            f"⚠️ Camera {names[0]} has no video (offline, unplugged or wrong login?). "
            f"Nothing is being recorded from it.{tail}"
        )
    return (
        f"⚠️ {len(names)} cameras have no video: {', '.join(names)}. Offline, unplugged, "
        f"a network switch down, or wrong logins? Nothing is being recorded from them.{tail}"
    )


def format_redeploy_result(data: dict) -> str:
    """The outcome written by deploy-edge-server.sh --auto."""
    status = data.get("status")
    old, new = str(data.get("from") or "?")[:7], str(data.get("to") or "?")[:7]
    detail = str(data.get("detail") or "").strip()
    tag = str(data.get("tag") or "").strip()
    release = f"release {tag} ({new})" if tag else new
    if status == "ok":
        if old == new:
            return f"✅ Redeployed. Already on the latest version ({release}); settings re-applied."
        return f"✅ Redeployed: updated {old} → {release}. All containers are running."
    if status == "rolled_back":
        return (
            f"⚠️ The update to {release} failed ({detail or 'unknown reason'}). Rolled back to "
            f"{old}; the stack is running on the old version."
        )
    if status == "refused":
        return f"Redeploy not started: {detail or 'unknown reason'}."
    return (
        f"❌ Redeploy failed ({detail or 'unknown reason'}) and the rollback did not work either. "
        "The server may be half-updated; see /var/log/edge-server-deploy.log."
    )


def compose_services(cfg: Config) -> list[str]:
    """Service names in the stack's docker-compose.yml (mounted read-only into the bot).
    The file is written by the deploy script, so a plain scan of its services: block is enough."""
    names: list[str] = []
    in_services = False
    for line in (read_text(cfg.app_dir / "docker-compose.yml") or "").splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
        elif in_services:
            if line and not line.startswith((" ", "#")):
                break  # the next top-level key
            match = re.match(r"^  ([A-Za-z0-9][A-Za-z0-9_-]*):\s*$", line)
            if match:
                names.append(match.group(1))
    return names


def format_restart_result(data: dict) -> str:
    """The outcome written by service-request.py restart."""
    service = str(data.get("service") or "the service")
    detail = str(data.get("detail") or "").strip()
    status = data.get("status")
    if status == "ok":
        return f"✅ Restarted {service}; it is running again." + (f" ({detail})" if detail else "")
    if status == "refused":
        return f"Restart not done: {detail or 'refused by the server'}."
    return (
        f"❌ Restarting {service} failed: {detail or 'unknown reason'}. "
        f"See /logs {service if data.get('service') else '<service>'}."
    )


def disk_problems(disk: dict, wear_limit: float) -> list[str]:
    """What is wrong with one drive, from smart-report.py's summary of it."""
    problems = []
    if disk.get("passed") is False:
        problems.append("SMART health check FAILED - the drive is failing")
    wear = disk.get("wear_used")
    if isinstance(wear, (int, float)) and wear_limit > 0 and wear >= wear_limit:
        problems.append(f"{wear:g}% of its rated life used (alert level {wear_limit:g}%)")
    reallocated, pending = disk.get("reallocated") or 0, disk.get("pending") or 0
    if reallocated or pending:
        problems.append(f"bad sectors: {reallocated} reallocated, {pending} pending")
    if disk.get("media_errors"):
        problems.append(f"{disk['media_errors']} media errors")
    return problems


@dataclass
class Bot:
    cfg: Config
    api: Telegram
    sleep: Callable[[float], None] = time.sleep
    _diag_lock: threading.Lock = field(default_factory=threading.Lock)
    _diag_last: float = 0.0
    threads: list[threading.Thread] = field(default_factory=list)
    _disk_state: dict[str, DiskState] = field(default_factory=dict)
    stop: threading.Event = field(default_factory=threading.Event)
    clock: Callable[[], float] = time.time
    _failures: dict[int, int] = field(default_factory=dict)
    _locked_until: dict[int, float] = field(default_factory=dict)
    _reboot_timer: threading.Timer | None = None
    _incidents: dict[str, Incident] = field(default_factory=dict)
    _farm_rules: list = field(default_factory=list)  # farm.Rule, parsed once from FARM_ALERTS
    _farm_rules_read: bool = False
    _active_jobs: set[str] = field(default_factory=set)
    _jobs_lock: threading.Lock = field(default_factory=threading.Lock)
    _logs_last: float = 0.0
    control: Any = None  # controlplane.ControlPlane when this node is in a fleet
    governance: Any = None  # governance.Governance: host actions through PDP and gateway
    assistant: Any = None  # assistant.Assistant: /ask (Autonomy M7)
    _governed: dict[str, str] = field(default_factory=dict)  # job kind -> action id

    # -- sending ----------------------------------------------------------

    def reply(self, chat_id: int, text: str, pre: bool = False) -> None:
        chunks: list[str] = []
        current = ""
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) > TELEGRAM_TEXT_LIMIT:
                chunks.append(current)
                current = ""
            current += line[:TELEGRAM_TEXT_LIMIT]
        chunks.append(current)
        for chunk in (c for c in chunks if c.strip()):
            payload: dict = {"chat_id": chat_id, "text": chunk}
            if pre:
                payload = {
                    "chat_id": chat_id,
                    "text": f"<pre>{html.escape(chunk, quote=False)}</pre>",
                    "parse_mode": "HTML",
                }
            try:
                self.api.call("sendMessage", payload)
            except TelegramError as e:
                log.warning("could not send message: %s", e)

    # -- commands ---------------------------------------------------------

    def cmd_status(self, chat_id: int) -> None:
        self.reply(chat_id, build_status(self.cfg, self.sleep), pre=True)

    def cmd_diag(self, chat_id: int) -> None:
        if not self._diag_lock.acquire(blocking=False):
            self.reply(chat_id, "A diagnostics run is already in progress.")
            return
        try:
            wait = self.cfg.diag_cooldown - (time.monotonic() - self._diag_last)
            if self._diag_last and wait > 0:
                self.reply(chat_id, f"Please wait {wait:.0f} s before asking again.")
                return
            seconds = self.cfg.diag_samples * self.cfg.diag_interval + 10
            self.reply(chat_id, f"Collecting diagnostics, about {seconds} s...")
            self._send_report(chat_id)
        finally:
            self._diag_last = time.monotonic()
            self._diag_lock.release()

    def cmd_testalert(self, chat_id: int, args: list[str]) -> None:
        """Send the low-disk alert with made-up numbers, to the requester only."""
        try:
            percent = float(args[0]) if args else 5.0
            if not 0 <= percent <= 100:
                raise ValueError
        except ValueError:
            self.reply(chat_id, "Usage: /testalert [free percent between 0 and 100]")
            return
        disks = watched_disks(self.cfg)
        label, path = disks[0] if disks else ("recordings", self.cfg.storage_path)
        usage = disk_usage(path)
        total = usage[0] if usage and usage[0] else 500 * 2**30
        threshold = self.cfg.alert_free_percent or 15.0
        self.reply(
            chat_id,
            format_disk_alert(label, int(total * percent / 100), total, threshold, test=True),
        )
        lines = ["Real state:"]
        for name, disk_path in disks:
            real = disk_usage(disk_path)
            if real and real[0]:
                lines.append(
                    f"  {name}: {gib(real[1])} of {gib(real[0])} GB free ({100 * real[1] / real[0]:.0f}%)"
                )
        if self.cfg.alert_free_percent > 0:
            lines.append(
                f"Alerts go to {len(self.cfg.allowed)} user(s) below "
                f"{self.cfg.alert_free_percent:g}% free, checked every "
                f"{self.cfg.alert_interval / 60:g} min."
            )
        else:
            lines.append("Disk alerts are turned off (ALERT_DISK_FREE_PERCENT=0).")
        self.reply(chat_id, "\n".join(lines))

    # -- disk alerts ------------------------------------------------------

    def disk_alert_messages(self, now: float | None = None) -> list[str]:
        """Alerts due now. Alerts once on crossing the level, reminds every
        ``alert_repeat`` seconds, and says so once when space is back."""
        threshold = self.cfg.alert_free_percent
        if threshold <= 0:
            return []
        now = time.monotonic() if now is None else now
        messages: list[str] = []
        for label, path in watched_disks(self.cfg):
            usage = disk_usage(path)
            if not usage or not usage[0]:
                continue
            total, free = usage
            pct = 100 * free / total
            state = self._disk_state.setdefault(label, DiskState())
            if pct < threshold:
                if not state.low or now - state.last_alert >= self.cfg.alert_repeat:
                    messages.append(
                        format_disk_alert(label, free, total, threshold, reminder=state.low)
                    )
                    state.last_alert = now
                state.low = True
            elif state.low and pct >= threshold + RECOVERY_MARGIN:
                state.low = False
                messages.append(
                    f"✅ Disk space recovered: {label} {gib(free)} of {gib(total)} GB free ({pct:.0f}%)."
                )
        return messages

    # -- watchers ---------------------------------------------------------
    # Each check calls judge() with what it found. A problem must be seen
    # ``alert_confirm`` times in a row before it is reported (a Frigate restart
    # or a camera reboot is not an outage), is repeated every ``alert_repeat``
    # seconds while it lasts, and a "back to normal" message ends it.

    def step(
        self,
        key: str,
        label: str,
        problem: str | None,
        now: float,
        confirm: int | None = None,
    ) -> str | None:
        """Advance one watched thing. Returns "alert", "reminder", "recovered" or None."""
        needed = self.cfg.alert_confirm if confirm is None else confirm
        incident = self._incidents.get(key)
        if problem is None:
            if incident is None:
                return None
            del self._incidents[key]
            return "recovered" if incident.alerted else None
        if incident is None:
            incident = self._incidents[key] = Incident(label)
        incident.strikes += 1
        if incident.strikes < needed:
            return None
        if incident.alerted and now - incident.last_alert < self.cfg.alert_repeat:
            return None
        event = "reminder" if incident.alerted else "alert"
        incident.alerted = True
        incident.last_alert = now
        return event

    def judge(
        self,
        key: str,
        label: str,
        problem: str | None,
        now: float,
        confirm: int | None = None,
    ) -> str | None:
        event = self.step(key, label, problem, now, confirm)
        if event == "recovered":
            return f"✅ {label}: back to normal."
        if event:
            return f"⚠️ {problem}" + (" (reminder)" if event == "reminder" else "")
        return None

    def watch_frigate(self, now: float) -> list[str]:
        cfg = self.cfg
        try:
            stats = json.loads(http_get(f"{cfg.frigate_url}/api/stats"))
        except (OSError, ValueError, urllib.error.URLError):
            message = self.judge(
                "frigate",
                "Frigate",
                "Frigate is not responding. Recording has probably stopped. "
                "If it does not recover by itself, reboot with /reboot.",
                now,
            )
            return [message] if message else []
        messages = [self.judge("frigate", "Frigate", None, now)]
        disabled = disabled_cameras(cfg)
        events: dict[str, list[str]] = {"alert": [], "reminder": [], "recovered": []}
        for name, camera in sorted((stats.get("cameras") or {}).items()):
            offline = name not in disabled and float(camera.get("camera_fps") or 0) < 0.5
            event = self.step(f"camera:{name}", name, "no video" if offline else None, now)
            if event:
                events[event].append(name)
        for event in ("alert", "reminder"):
            if events[event]:
                messages.append(no_video_message(events[event], reminder=event == "reminder"))
        if events["recovered"]:
            names = events["recovered"]
            messages.append(
                f"✅ Camera {names[0]}: back to normal."
                if len(names) == 1
                else f"✅ Cameras back to normal: {', '.join(names)}."
            )
        return [m for m in messages if m]

    def watch_processor(self, now: float) -> list[str]:
        stats, error = processor_stats(self.cfg)
        message = self.judge(
            "processor", "Job processor", processor_problem(self.cfg, stats, error), now
        )
        return [message] if message else []

    def watch_temperature(self, now: float) -> list[str]:
        limit = self.cfg.alert_temp_c
        temps = temperatures(self.cfg.sys)
        if limit <= 0 or not temps:
            return []
        name, hottest = max(temps, key=lambda t: t[1])
        if hottest >= limit:
            problem = (
                f"CPU temperature {hottest:.0f} °C ({name}), alert level {limit:g} °C. "
                "Check fan, dust and airflow."
            )
        elif hottest < limit - 5:
            problem = None
        else:
            return []  # in the hysteresis band: no change either way
        message = self.judge("temperature", "CPU temperature", problem, now)
        return [message] if message else []

    def watch_disk_health(self, now: float) -> list[str]:
        path = self.cfg.status_dir / "disks.json"
        try:
            report = json.loads(path.read_text())
            generated = float(report["generated"])
        except FileNotFoundError:
            return []  # not installed (no smartmontools or no bot-status mount)
        except (OSError, ValueError, KeyError, TypeError):
            report, generated = {"error": "disks.json is unreadable"}, self.clock()
        messages = []
        age = self.clock() - generated
        monitor_problem = None
        if report.get("error"):
            monitor_problem = f"Disk health monitor problem: {report['error']}."
        elif age > self.cfg.smart_max_age:
            monitor_problem = (
                f"Disk health has not been checked for {human_duration(age)} "
                "(systemctl status edge-smart.timer edge-smart.service)."
            )
        messages.append(self.judge("smart:monitor", "Disk health monitor", monitor_problem, now, 1))
        for disk in report.get("disks") or []:
            device = str(disk.get("device", "?"))
            found = disk_problems(disk, self.cfg.alert_wear_percent)
            title = f"{device} ({disk.get('model') or 'unknown model'})"
            problem = None
            if found:
                problem = (
                    f"Disk {title}: " + "; ".join(found) + ". Plan a replacement and keep "
                    "a copy of the config (.cam, .telegram, frigate/config.yml)."
                )
            messages.append(self.judge(f"smart:{device}", f"Disk {title}", problem, now, 1))
        return [m for m in messages if m]

    def watch_farm(self, now: float) -> list[str]:
        """The Field Station cell (Autonomy M10): is it reporting, and are the readings you
        chose to watch (FARM_ALERTS) inside their limits. Reads only the cell's status file."""
        cfg = self.cfg
        if cfg.farm_status is None:
            return []
        import farm  # next to this file; standard library only

        confirm = cfg.farm_alert_confirm or None
        doc = farm.load(cfg.farm_status)
        messages: list[str | None] = []
        problem = farm.station_problem(doc, self.clock(), cfg.farm_stale)
        if cfg.farm_alert_station:
            messages.append(self.judge("farm:station", "Field Station", problem, now, confirm))
        if cfg.farm_alerts and not self._farm_rules_read:
            self._farm_rules, bad = farm.parse_rules(cfg.farm_alerts)
            self._farm_rules_read = True
            for entry in bad:
                log.warning(
                    "FARM_ALERTS: cannot read %r (use entity<number or entity>number)", entry
                )
        if doc is None or problem is not None:
            # readings from a cell that is not reporting say nothing; the station alert covers it
            return [m for m in messages if m]
        for rule in self._farm_rules:
            state, why = farm.rule_state(rule, doc)
            if state == "band":
                continue
            label = farm.label(doc, rule.entity)
            messages.append(
                self.judge(
                    f"farm:rule:{rule.text}", label, why if state == "bad" else None, now, confirm
                )
            )
        return [m for m in messages if m]

    def watch_messages(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        messages: list[str] = []
        checks = [self.watch_frigate, self.watch_temperature, self.watch_disk_health]
        if self.cfg.processor_url:
            checks.append(self.watch_processor)
        if self.cfg.farm_status is not None:
            checks.append(self.watch_farm)
        for check in checks:
            try:
                messages += check(now)
            except Exception:  # noqa: BLE001 - one broken check must not hide the others
                log.exception("%s failed", check.__name__)
        return messages

    def broadcast(self, messages: list[str]) -> None:
        for text in messages:
            for chat_id in sorted(self.cfg.allowed):  # private chats: chat id == user id
                self.reply(chat_id, text)

    def send_alerts(self) -> None:
        self.broadcast(self.disk_alert_messages())

    def send_watch_alerts(self) -> None:
        if self.cfg.watch:
            self.broadcast(self.watch_messages())

    def alert_loop(self) -> None:
        while True:
            try:
                self.send_alerts()
            except Exception:  # noqa: BLE001 - keep watching
                log.exception("disk check failed")
            if self.stop.wait(self.cfg.alert_interval):
                return

    def watch_loop(self) -> None:
        while True:
            try:
                self.send_watch_alerts()
            except Exception:  # noqa: BLE001 - keep watching
                log.exception("watcher run failed")
            if self.stop.wait(self.cfg.watch_interval):
                return

    # -- reboot -----------------------------------------------------------
    # The container can only drop a file into requests_dir. A systemd path unit
    # on the host notices it and reboots; nothing here can run commands on the host.

    def commands(self) -> list[dict]:
        commands = list(COMMANDS)
        if self.control:
            commands.append(FLEET_COMMAND)
        if self.cfg.totp_secret:
            commands += [REBOOT_COMMAND, REDEPLOY_COMMAND, RESTART_COMMAND, LOGS_COMMAND]
        if self.assistant is not None:
            commands.append(ASK_COMMAND)
        if self.cfg.farm_status is not None:
            commands.append(FARM_COMMAND)
        return commands

    def help_text(self) -> str:
        extra = REBOOT_HELP + REDEPLOY_HELP + RESTART_HELP + LOGS_HELP
        text = HELP + (FLEET_HELP if self.control else "") + (AUDIT_HELP if self.governance else "")
        text += ASK_HELP if self.assistant is not None else ""
        text += FARM_HELP if self.cfg.farm_status is not None else ""
        return text + (extra if self.cfg.totp_secret else "")

    def setup_assistant(self) -> None:
        """/ask (Autonomy M7): a node assistant on the model gateway, when a model route is
        configured (MODELS_CONFIG, or the job processor's llm.chat). Never stops the bot."""
        cfg = self.cfg
        try:
            import assistant

            state = cfg.control_state if cfg.control_state.is_dir() else None
            models = assistant.NodeModels(
                processor_url=cfg.processor_url,
                key_file=cfg.processor_api_key_file,
                key=cfg.processor_api_key,
                model=cfg.llm_model,
                config_file=cfg.models_config,
                usage_path=state / "model-usage.jsonl" if state else None,
                tracer=getattr(self.governance, "tracer", None),
            )
            if not models.configured:
                return
            self.assistant = assistant.Assistant(
                models,
                tools={
                    "node.status": lambda: build_status(cfg, self.sleep),
                    "node.audit": lambda: (
                        self.governance.audit_text(15) if self.governance else "audit log off"
                    ),
                    "frigate.cameras": lambda: "\n".join(frigate_status(cfg)[0]),
                },
                services=lambda: [s for s in compose_services(cfg) if s not in HIDDEN_FROM_RESTART],
                governance=self.governance,
                cell=getattr(self.governance, "cell", "local"),
                clock=self.clock,
            )
            if cfg.model_review and self.governance is not None:
                self.governance.use_reviewer(models)
        except Exception as e:  # noqa: BLE001 - /ask is optional
            log.warning("/ask off: %s", e)
            self.assistant = None
            return
        log.info("/ask on%s", " (LLM review of governed actions on)" if cfg.model_review else "")

    def cmd_ask(self, chat_id: int, args: list[str]) -> None:
        if self.assistant is None:
            self.reply(
                chat_id, "/ask is off: no model is configured (INSTALL_PROCESSOR or MODELS_CONFIG)."
            )
            return
        question = " ".join(args).strip()
        if not question:
            self.reply(chat_id, "Send /ask <question>, e.g. /ask why are detections slow?")
            return
        self.reply(chat_id, "Thinking (the model runs on the GPU machine; up to a few minutes)...")
        try:
            answer = self.assistant.ask(question[:1000])
        except Exception as e:  # noqa: BLE001 - always answer
            log.exception("/ask failed")
            answer = f"The assistant failed: {e}"
        self.reply(chat_id, answer)

    def cmd_farm(self, chat_id: int) -> None:
        if self.cfg.farm_status is None:
            self.reply(
                chat_id, "/farm is off: this node has no Field Station cell (FARM_STATUS_FILE)."
            )
            return
        try:
            import farm  # next to this file; standard library only

            text = farm.farm_text(self.cfg.farm_status, self.clock, self.cfg.farm_stale)
        except Exception as e:  # noqa: BLE001 - always answer
            log.exception("/farm failed")
            text = f"/farm failed: {e}"
        self.reply(chat_id, text, pre=True)

    def cmd_fleet(self, chat_id: int) -> None:
        if not self.control:
            self.reply(
                chat_id,
                "This node is not part of a fleet: no fleet file with two or more nodes was "
                "deployed here (README: Fleet file).",
            )
            return
        self.reply(chat_id, self.control.fleet_text(), pre=True)

    def setup_governance(self) -> None:
        """Route /restart, /reboot and /redeploy through the policy decision point and the
        action gateway (Autonomy M5) when autonomy-core and a state directory are here."""
        cfg = self.cfg
        if not cfg.totp_secret or not cfg.requests_dir.is_dir():
            return
        if not cfg.control_state.is_dir():
            log.info("governed actions off: no state directory at %s", cfg.control_state)
            return
        try:
            import governance

            key = governance.load_or_create_key(
                cfg.control_key if cfg.control_key.exists() else cfg.control_state / "gateway.key"
            )
            # a fleet's verified registry, else the one an owner put in the state directory:
            # a one-node install can then also hold an owner-signed policy bundle (grants)
            registry = getattr(self.control, "registry", None)
            if registry is None:
                registry = governance.core.RegistryStore(cfg.control_state / "registry").current(
                    now=self.clock()
                )
            self.governance = governance.Governance(
                cfg.control_state,
                key=key,
                cell=governance.cell_name({"node": self.control.me} if self.control else None),
                submit={
                    "restart": lambda payload: self._submit_job(
                        "restart", {"service": payload["service"]}
                    ),
                    "reboot": lambda payload: self._create_request_once("reboot"),
                    "redeploy": lambda payload: self._create_request_once("redeploy"),
                },
                registry=registry,
                bundle_file=cfg.control_state / "policy-bundle.json",
                clock=self.clock,
                operators=cfg.allowed,
                boot_time=self.boot_time,
                frigate_started=self.frigate_started,
                services=lambda: compose_services(cfg),
            )
        except Exception as e:  # noqa: BLE001 - the bot keeps the direct path
            log.warning("governed actions off (%s): %s", type(e).__name__, e)
            self.governance = None
            return
        log.info(
            "governed actions on: cell %s, policy bundle %s; outcomes verified every %g s",
            self.governance.cell,
            self.governance.pdp.bundle.version,
            VERIFY_INTERVAL,
        )

    def _create_request_once(self, name: str) -> bool:
        try:
            self._create_request(name)
        except FileExistsError:
            return False
        return True

    def _govern(
        self, chat_id: int, user_id: int, message: dict, action: str, target: str, kind: str
    ) -> bool:
        """Propose, decide and execute through the gateway; reply on refusal. True when the
        host helper has the request."""
        try:
            rec = self.governance.request(
                action,
                target,
                requested_by=user_id,
                message_id=message.get("message_id"),
                private_chat=(message.get("chat") or {}).get("type") == "private",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("governed %s failed", kind)
            self.reply(chat_id, f"Could not request the {kind}: {e}")
            return False
        if rec["status"] == "rejected":
            err = rec.get("error", {})
            self.reply(chat_id, f"Not done: {err.get('code')}: {err.get('message')}")
            return False
        if rec["status"] == "failed":
            self.reply(chat_id, f"A {kind} is already pending.")
            return False
        self._governed[kind] = rec["action_id"]
        return True

    def _governed_done(self, kind: str, ok: bool, detail: str = "") -> None:
        if self.governance is None:
            return
        aid = self._governed.pop(kind, None) or self.governance.pending(kind)
        try:
            self.governance.finished(aid, ok, detail)
        except Exception:  # noqa: BLE001 - the owner still gets the result
            log.exception("could not record the %s outcome", kind)
        self.verify_actions()

    def verify_actions(self, final: bool = False) -> None:
        """Autonomy M8 (C14): check executed actions against what they should have done; tell
        the owner when an outcome could not be confirmed or was contradicted."""
        if self.governance is None:
            return
        try:
            results = self.governance.verify_pending(final=final)
        except Exception:  # noqa: BLE001 - verification must not break the bot
            log.exception("verification failed")
            return
        for v in results:
            log.info("verification %s: %s", v["action_id"], v["result"])
            if v["result"] != "confirmed":
                self.broadcast([self.governance.verification_text(v)])

    def verify_loop(self) -> None:
        while not self.stop.wait(VERIFY_INTERVAL):
            self.verify_actions()

    def boot_time(self) -> float | None:
        """When the host booted (the container shares the host kernel's /proc/uptime)."""
        up = self.uptime()
        return None if up is None else self.clock() - up

    def frigate_started(self) -> float | None:
        """When Frigate's process started, from Frigate's own API (independent of the host
        helper that restarted it)."""
        try:
            stats = json.loads(http_get(f"{self.cfg.frigate_url}/api/stats"))
            up = float((stats.get("service") or {})["uptime"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return self.clock() - up

    def _governance_on(self, chat_id: int) -> bool:
        if self.governance is None:
            self.reply(
                chat_id,
                "Approvals and controls are off on this node (needs /reboot set up and autonomy-core).",
            )
            return False
        return True

    def cmd_pending(self, chat_id: int) -> None:
        if self._governance_on(chat_id):
            self.reply(chat_id, self.governance.pending_text())

    def cmd_approve(self, chat_id: int, user_id: int, message: dict, args: list[str]) -> None:
        """/approve <id> <code>: the owner approves a waiting action (IT-12)."""
        if not self._governance_on(chat_id):
            return
        if len(args) < 2:
            self.reply(chat_id, "Send /approve <id> <code>. /pending lists what is waiting.")
            return
        if self._totp_gate(chat_id, user_id, message, args[1:], "approve", lambda: None) is None:
            return
        try:
            rec = self.governance.approve(
                args[0],
                operator=user_id,
                private_chat=(message.get("chat") or {}).get("type") == "private",
            )
        except Exception as e:  # noqa: BLE001 - Reject explains itself
            self.reply(chat_id, f"Not approved: {e}")
            return
        if rec["status"] == "rejected":
            err = rec.get("error", {})
            self.reply(
                chat_id,
                f"Approved, but the gateway refused it: {err.get('code')}: {err.get('message')}",
            )
        else:
            self.reply(chat_id, f"Approved; the action is {rec['status']}.")

    def cmd_deny(self, chat_id: int, user_id: int, args: list[str]) -> None:
        if not self._governance_on(chat_id):
            return
        if not args:
            self.reply(chat_id, "Send /deny <id>. /pending lists what is waiting.")
        elif self.governance.deny(args[0], user_id):
            self.reply(chat_id, f"Denied {args[0]}; it will not run.")
        else:
            self.reply(chat_id, f"{args[0]} is not waiting (answered or expired).")

    def cmd_suspend(self, chat_id: int, user_id: int, args: list[str], kind: str) -> None:
        """/suspend <scope> [reason] and /hold [reason]: stop automatic actions at once. No code:
        making things safer must be quick; resuming needs one."""
        if not self._governance_on(chat_id):
            return
        if kind == "suspend" and not args:
            self.reply(
                chat_id, "Send /suspend <all | cell | component id> [reason], or /hold [reason]."
            )
            return
        scope = "all" if kind == "hold" else args[0]
        if scope == "cell":
            scope = f"cell:{self.governance.cell}"
        reason = " ".join(args if kind == "hold" else args[1:]) or kind
        try:
            o = self.governance.order(kind, scope, reason, user_id)
        except Exception as e:  # noqa: BLE001
            self.reply(chat_id, f"Not done: {e}")
            return
        self.reply(
            chat_id,
            f"{o['kind'].capitalize()} {o['scope']}: automatic actions are refused until /resume. "
            "Owner commands still work.",
        )

    def cmd_resume(self, chat_id: int, user_id: int, message: dict, args: list[str]) -> None:
        """/resume [scope] <code>"""
        if not self._governance_on(chat_id):
            return
        scope = args[0] if len(args) >= 2 else "all"
        if scope == "cell":
            scope = f"cell:{self.governance.cell}"
        if self._totp_gate(chat_id, user_id, message, args[-1:], "resume", lambda: None) is None:
            return
        self.governance.order("resume", scope, "resumed by the owner", user_id)
        self.reply(chat_id, f"Resumed {scope}.")

    def cmd_audit(self, chat_id: int) -> None:
        if self.governance is None:
            self.reply(
                chat_id,
                "The audit log is off on this node (needs /reboot set up and autonomy-core).",
            )
            return
        self.reply(chat_id, self.governance.audit_text(), pre=True)

    def setup_control(self) -> None:
        """Start the control plane if the host described a fleet. Never stops the bot."""
        config_path = self.cfg.status_dir / "fleet.json"
        if not config_path.is_file():
            return
        try:
            import controlplane  # next to this file; needs cryptography and autonomy-core

            config = controlplane.load_config(config_path)
            key = controlplane.load_private_key(self.cfg.control_key)
            boot = (read_text(self.cfg.proc / "sys/kernel/random/boot_id") or "").strip()
            plane = controlplane.ControlPlane(
                config,
                key,
                self.cfg.control_state,
                uptime=lambda: self.uptime() or 0.0,
                boot_id=boot,
                clock=self.clock,
            )
            plane.start(self.cfg.control_bind)
        except Exception as e:  # a broken fleet setup must not take /status down with it
            log.error("fleet control plane is off: %s: %s", type(e).__name__, e)
            return
        self.control = plane
        log.info(
            "fleet control plane on: node %s, public key %s", plane.me, controlplane.public_b64(key)
        )

    def uptime(self) -> float | None:
        text = read_text(self.cfg.proc / "uptime")
        try:
            return float((text or "").split()[0])
        except (IndexError, ValueError):
            return None

    def _step_file(self) -> Path:
        return self.cfg.requests_dir / "totp-last-step"

    def _load_last_step(self) -> int:
        try:
            return int(self._step_file().read_text().strip())
        except (OSError, ValueError):
            return 0

    def _save_last_step(self, step: int) -> None:
        tmp = self._step_file().with_suffix(".tmp")
        tmp.write_text(str(step))
        os.replace(tmp, self._step_file())

    def _totp_gate(
        self,
        chat_id: int,
        user_id: int,
        message: dict,
        args: list[str],
        command: str,
        precheck: Callable[[], str | None],
    ) -> float | None:
        """Everything that must be true before a dangerous command acts. Returns the
        time the code was accepted, or None after having told the user why not.

        The same authenticator code, replay record and lockout serve /reboot and
        /redeploy, so a code used for one cannot be used for the other."""
        cfg = self.cfg
        if not cfg.totp_secret or not cfg.requests_dir.is_dir():
            self.reply(
                chat_id,
                f"/{command} is not set up. Run 'python3 telegram-bot/bot.py --totp-setup' on "
                "the server, put REBOOT_TOTP_SECRET in .telegram and redeploy.",
            )
            return None
        if (message.get("chat") or {}).get("type") != "private":
            self.reply(chat_id, f"Use /{command} in a private chat with the bot, not in a group.")
            return None
        if not args:
            self.reply(
                chat_id,
                f"Send /{command} followed by the 6-digit code from your authenticator app.",
            )
            return None
        now = self.clock()
        if now < self._locked_until.get(user_id, 0.0):
            wait = int(self._locked_until[user_id] - now) // 60 + 1
            self.reply(chat_id, f"Too many wrong codes. Try again in about {wait} min.")
            return None
        if now - float(message.get("date") or 0) > TOTP_FILE_AGE:
            self.reply(chat_id, f"That message is too old. Send /{command} with a fresh code.")
            return None
        code = args[0]
        if not re.fullmatch(r"\d{6}", code):
            self.reply(chat_id, f"The code is 6 digits: /{command} 123456")
            return None
        refusal = precheck()
        if refusal:
            self.reply(chat_id, refusal)
            return None

        step = verify_totp(cfg.totp_secret, code, now, self._load_last_step())
        if step is None:
            failures = self._failures.get(user_id, 0) + 1
            if failures >= REBOOT_MAX_FAILURES:
                self._failures.pop(user_id, None)
                self._locked_until[user_id] = now + REBOOT_LOCKOUT
                log.warning(
                    "%s: user %s locked out after %d wrong codes", command, user_id, failures
                )
                self.reply(chat_id, "Wrong code. Locked for 15 minutes.")
            else:
                self._failures[user_id] = failures
                log.warning("%s: wrong code from user %s (%d)", command, user_id, failures)
                self.reply(chat_id, f"Wrong code ({failures}/{REBOOT_MAX_FAILURES}).")
            return None
        self._failures.pop(user_id, None)

        try:
            self._save_last_step(step)  # before acting: this is what stops a replay
        except OSError as e:
            log.error("%s refused: cannot store the used code: %s", command, e)
            self.reply(chat_id, "Not proceeding: cannot write the requests folder.")
            return None
        try:
            self.api.call(
                "deleteMessage", {"chat_id": chat_id, "message_id": message.get("message_id")}
            )
        except TelegramError as e:
            log.info("could not delete the code message: %s", e)
        return now

    def _create_request(self, name: str) -> None:
        fd = os.open(self.cfg.requests_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)

    def cmd_reboot(self, chat_id: int, user_id: int, message: dict, args: list[str]) -> None:
        cfg = self.cfg

        def precheck() -> str | None:
            up = self.uptime()
            if up is None or up < cfg.reboot_min_uptime:
                shown = "unknown" if up is None else human_duration(up)
                return (
                    f"Not rebooting: uptime is {shown}. The laptop must have been up for "
                    f"{human_duration(cfg.reboot_min_uptime)} (protects against reboot loops)."
                )
            if (cfg.requests_dir / "reboot").exists():
                return "A reboot is already pending."
            return None

        now = self._totp_gate(chat_id, user_id, message, args, "reboot", precheck)
        if now is None:
            return
        marker = cfg.requests_dir / "reboot-notify"
        try:
            marker.write_text(json.dumps({"chat_id": chat_id, "at": now}))
            if self.governance is not None:
                if not self._govern(chat_id, user_id, message, "host.reboot", "host", "reboot"):
                    marker.unlink(missing_ok=True)
                    return
                self.reply(
                    chat_id, "Code accepted. Rebooting now; I will message you when I am back."
                )
            else:
                self.reply(
                    chat_id, "Code accepted. Rebooting now; I will message you when I am back."
                )
                self._create_request("reboot")
        except FileExistsError:
            self.reply(chat_id, "A reboot is already pending.")
            return
        except OSError as e:
            log.error("reboot request failed: %s", e)
            marker.unlink(missing_ok=True)
            self.reply(chat_id, f"Could not request the reboot: {e}")
            return
        log.warning("reboot requested by user id %s", user_id)
        timer = threading.Timer(cfg.reboot_wait, self._reboot_not_done, args=(chat_id,))
        timer.daemon = True
        self._reboot_timer = timer
        timer.start()

    # -- redeploy ---------------------------------------------------------
    # Same drop box as /reboot. The host unit edge-redeploy.path runs
    # "deploy-edge-server.sh --auto" (fetch GitHub main, require green CI, deploy,
    # verify, roll back on failure) and writes redeploy-result when done. The bot
    # may be recreated during the deploy, so a new instance resumes waiting.

    def _redeploy_in_progress(self) -> bool:
        if (self.cfg.requests_dir / "redeploy").exists():
            return True
        try:
            at = float(json.loads((self.cfg.requests_dir / "redeploy-notify").read_text())["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return self.clock() - at < self.cfg.redeploy_max_wait

    def cmd_redeploy(self, chat_id: int, user_id: int, message: dict, args: list[str]) -> None:
        cfg = self.cfg

        def precheck() -> str | None:
            if self._redeploy_in_progress():
                return "A redeploy is already in progress. I will message you when it is done."
            if (cfg.requests_dir / "reboot").exists():
                return "A reboot is pending; try again after it."
            return None

        now = self._totp_gate(chat_id, user_id, message, args, "redeploy", precheck)
        if now is None:
            return
        marker = cfg.requests_dir / "redeploy-notify"
        (cfg.requests_dir / "redeploy-result").unlink(missing_ok=True)  # an old one
        try:
            marker.write_text(json.dumps({"chat_id": chat_id, "at": now}))
            if self.governance is not None:
                if not self._govern(chat_id, user_id, message, "host.redeploy", "host", "redeploy"):
                    marker.unlink(missing_ok=True)
                    return
            else:
                self._create_request("redeploy")
        except FileExistsError:
            self.reply(chat_id, "A redeploy is already in progress.")
            return
        except OSError as e:
            log.error("redeploy request failed: %s", e)
            marker.unlink(missing_ok=True)
            self.reply(chat_id, f"Could not request the redeploy: {e}")
            return
        log.warning("redeploy requested by user id %s", user_id)
        self.reply(
            chat_id,
            "Code accepted. Deploying the newest release tag and redeploying; this can take several "
            "minutes (images, checks, restart). It rolls back by itself if the new version does not "
            "come up. I will message you with the outcome.",
        )
        self._start_redeploy_watch(chat_id)

    # -- restart and logs -------------------------------------------------
    # Same drop box. The container writes bot-requests/<kind>-request with a service
    # name; the host unit (edge-restart.path / edge-logs.path) runs service-request.py,
    # which only accepts services from the stack's compose file, and writes
    # <kind>-result. The bot never gets Docker access.

    def _host_jobs_available(self, chat_id: int, command: str) -> bool:
        if self.cfg.totp_secret and self.cfg.requests_dir.is_dir():
            return True
        self.reply(
            chat_id,
            f"/{command} is not set up. It uses the same host helper as /reboot: run 'python3 "
            "telegram-bot/bot.py --totp-setup' on the server, put REBOOT_TOTP_SECRET in .telegram "
            "and redeploy.",
        )
        return False

    def _submit_job(self, kind: str, payload: dict) -> bool:
        """Drop the request for the host. False if one is already waiting."""
        cfg = self.cfg
        for stale in (f"{kind}-result", "logs-output.txt" if kind == "logs" else ""):
            if stale:
                (cfg.requests_dir / stale).unlink(missing_ok=True)
        tmp = cfg.requests_dir / f".{kind}-request.tmp"
        tmp.write_text(json.dumps(payload))
        try:
            os.link(tmp, cfg.requests_dir / f"{kind}-request")  # fails if one is pending
        except FileExistsError:
            return False
        finally:
            tmp.unlink(missing_ok=True)
        return True

    def _job_busy(self, kind: str) -> bool:
        with self._jobs_lock:
            return kind in self._active_jobs or (self.cfg.requests_dir / f"{kind}-request").exists()

    def _start_job_watch(
        self, kind: str, chat_id: int, max_wait: float, deliver: Callable[[int], bool]
    ) -> None:
        with self._jobs_lock:
            self._active_jobs.add(kind)
        thread = threading.Thread(
            target=self._watch_job, args=(kind, chat_id, max_wait, deliver), daemon=True
        )
        thread.start()
        self.threads.append(thread)

    def _watch_job(
        self, kind: str, chat_id: int, max_wait: float, deliver: Callable[[int], bool]
    ) -> None:
        cfg = self.cfg
        request = cfg.requests_dir / f"{kind}-request"
        start_polls = int(cfg.job_start_wait / cfg.job_poll)
        try:
            for i in range(max(1, int(max_wait / cfg.job_poll))):
                if deliver(chat_id):
                    return
                if i >= start_polls and request.exists():
                    request.unlink(missing_ok=True)
                    self.reply(
                        chat_id,
                        f"The host did not pick up the {kind} request. On the server check: "
                        f"systemctl status edge-{kind}.path edge-{kind}.service",
                    )
                    return
                self.sleep(cfg.job_poll)
            self.reply(chat_id, f"No answer to /{kind} after {human_duration(max_wait)}.")
        finally:
            with self._jobs_lock:
                self._active_jobs.discard(kind)

    def _claim_result(self, kind: str) -> dict | None:
        result = self.cfg.requests_dir / f"{kind}-result"
        claimed = result.with_name(f"{kind}-result.claimed")
        try:
            os.rename(result, claimed)
        except OSError:
            return None
        try:
            data = json.loads(claimed.read_text())
        except (OSError, ValueError):
            data = {}
        claimed.unlink(missing_ok=True)
        return data if isinstance(data, dict) else {}

    def cmd_restart(self, chat_id: int, user_id: int, message: dict, args: list[str]) -> None:
        cfg = self.cfg
        services = [s for s in compose_services(cfg) if s not in HIDDEN_FROM_RESTART]
        service = args[0].lower() if args else ""
        if cfg.totp_secret and (len(args) < 2 or service not in services):
            known = ", ".join(services) or "none found"
            if service and len(args) >= 2:
                self.reply(chat_id, f"Unknown service '{service}'. Services: {known}")
            else:
                self.reply(
                    chat_id,
                    f"Send /restart <service> <code>, with the 6-digit code from your "
                    f"authenticator app. Services: {known}",
                )
            return  # nothing was spent: a wrong name does not use up or count a code

        def precheck() -> str | None:
            if self._job_busy("restart"):
                return "A restart is already in progress. I will message you when it is done."
            for pending in ("reboot", "redeploy"):
                if (cfg.requests_dir / pending).exists():
                    return f"A {pending} is pending; try again after it."
            return None

        now = self._totp_gate(chat_id, user_id, message, args[1:], "restart", precheck)
        if now is None:
            return
        try:
            if self.governance is not None:
                if not self._govern(
                    chat_id, user_id, message, "host.restart_service", service, "restart"
                ):
                    return
            elif not self._submit_job("restart", {"service": service}):
                self.reply(chat_id, "A restart is already in progress.")
                return
        except OSError as e:
            log.error("restart request failed: %s", e)
            self.reply(chat_id, f"Could not request the restart: {e}")
            return
        log.warning("restart of %s requested by user id %s", service, user_id)
        self.reply(
            chat_id, f"Code accepted. Restarting {service}; I will message you when it is up."
        )
        self._start_job_watch("restart", chat_id, cfg.restart_max_wait, self._deliver_restart)

    def _deliver_restart(self, chat_id: int) -> bool:
        data = self._claim_result("restart")
        if data is None:
            return False
        self._governed_done("restart", data.get("status") == "ok", str(data.get("detail") or ""))
        self.reply(chat_id, format_restart_result(data))
        return True

    def cmd_logs(self, chat_id: int, args: list[str]) -> None:
        cfg = self.cfg
        if not self._host_jobs_available(chat_id, "logs"):
            return
        services = compose_services(cfg)
        service = args[0].lower() if args else ""
        lines = 100
        if len(args) > 1:
            try:
                lines = int(args[1])
            except ValueError:
                lines = 0
        if service not in services or not 1 <= lines <= 1000:
            self.reply(
                chat_id,
                f"Usage: /logs <service> [lines 1-1000]. Services: {', '.join(services) or 'none found'}",
            )
            return
        wait = cfg.logs_cooldown - (self.clock() - self._logs_last)
        if self._logs_last and wait > 0:
            self.reply(chat_id, f"Please wait {wait:.0f} s before asking again.")
            return
        if self._job_busy("logs"):
            self.reply(chat_id, "Logs are already being fetched.")
            return
        try:
            if not self._submit_job("logs", {"service": service, "lines": lines}):
                self.reply(chat_id, "Logs are already being fetched.")
                return
        except OSError as e:
            log.error("logs request failed: %s", e)
            self.reply(chat_id, f"Could not request the logs: {e}")
            return
        self._logs_last = self.clock()
        self._start_job_watch("logs", chat_id, cfg.logs_max_wait, self._deliver_logs)

    def _deliver_logs(self, chat_id: int) -> bool:
        data = self._claim_result("logs")
        if data is None:
            return False
        service = str(data.get("service") or "service")
        output = self.cfg.requests_dir / "logs-output.txt"
        if data.get("status") != "ok":
            output.unlink(missing_ok=True)
            self.reply(chat_id, f"Logs not available: {data.get('detail') or 'unknown reason'}.")
            return True
        try:
            text = output.read_bytes().decode(errors="replace")
        except OSError:
            text = ""
        output.unlink(missing_ok=True)
        if not text.strip():
            self.reply(chat_id, f"{service}: no log lines.")
        elif len(text) <= LOGS_INLINE_LIMIT:
            self.reply(chat_id, f"{service} (last {data.get('lines')} lines)\n{text}", pre=True)
        else:
            name = time.strftime(f"{service}-logs-%Y%m%d-%H%M%S.txt")
            try:
                self.api.send_document(
                    chat_id,
                    name,
                    text.encode(),
                    caption=f"{service}, last {data.get('lines')} lines. Passwords are removed.",
                )
            except TelegramError as e:
                log.error("could not send logs: %s", e)
                self.reply(chat_id, "Could not upload the logs to Telegram.")
        return True

    def _start_redeploy_watch(self, chat_id: int | None) -> None:
        thread = threading.Thread(target=self._watch_redeploy, args=(chat_id,), daemon=True)
        thread.start()
        self.threads.append(thread)

    def _watch_redeploy(self, chat_id: int | None) -> None:
        cfg = self.cfg
        request = cfg.requests_dir / "redeploy"
        start_polls = int(cfg.redeploy_start_wait / cfg.redeploy_poll)
        for i in range(int(cfg.redeploy_max_wait / cfg.redeploy_poll)):
            if self.deliver_redeploy_result():
                return
            if i >= start_polls and request.exists():
                request.unlink(missing_ok=True)
                (cfg.requests_dir / "redeploy-notify").unlink(missing_ok=True)
                self._tell(
                    chat_id,
                    "The redeploy did not start. On the server check: "
                    "systemctl status edge-redeploy.path edge-redeploy.service",
                )
                return
            self.sleep(cfg.redeploy_poll)
        (cfg.requests_dir / "redeploy-notify").unlink(missing_ok=True)
        self._tell(
            chat_id,
            "No redeploy result after "
            f"{human_duration(cfg.redeploy_max_wait)}. Check /var/log/edge-server-deploy.log.",
        )

    def _tell(self, chat_id: int | None, text: str) -> None:
        for target in [chat_id] if chat_id else sorted(self.cfg.allowed):
            self.reply(target, text)

    def deliver_redeploy_result(self) -> bool:
        """Send the host's redeploy outcome, once. True if there was one."""
        cfg = self.cfg
        result = cfg.requests_dir / "redeploy-result"
        claimed = result.with_name("redeploy-result.claimed")
        try:
            os.rename(result, claimed)  # whoever renames it first reports it
        except OSError:
            return False
        try:
            data = json.loads(claimed.read_text())
        except (OSError, ValueError):
            data = {}
        claimed.unlink(missing_ok=True)
        chat_id: int | None = None
        marker = cfg.requests_dir / "redeploy-notify"
        try:
            chat_id = int(json.loads(marker.read_text())["chat_id"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        marker.unlink(missing_ok=True)
        try:
            age = self.clock() - float(data.get("at") or 0)
        except (TypeError, ValueError):
            age = 0.0
        if data.get("at") and age > REDEPLOY_RESULT_MAX_AGE:
            return True  # a leftover from long ago
        self._governed_done("redeploy", data.get("status") == "ok", str(data.get("detail") or ""))
        self._tell(chat_id, format_redeploy_result(data))
        return True

    def resume_redeploy(self) -> None:
        """At start-up: report a finished redeploy, or keep waiting for a running one
        (the deploy recreates this container when the bot's files changed)."""
        if self.deliver_redeploy_result():
            return
        if self._redeploy_in_progress():
            chat_id = None
            try:
                chat_id = int(
                    json.loads((self.cfg.requests_dir / "redeploy-notify").read_text())["chat_id"]
                )
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self._start_redeploy_watch(chat_id)

    def _reboot_not_done(self, chat_id: int) -> None:
        self._governed_done("reboot", False, "the host did not reboot")
        for name in ("reboot", "reboot-notify"):
            (self.cfg.requests_dir / name).unlink(missing_ok=True)
        self.reply(
            chat_id,
            "The laptop did not reboot. On the server check: systemctl status edge-reboot.path "
            "edge-reboot.service (it also refuses within 10 minutes of boot).",
        )

    def notify_after_reboot(self) -> None:
        marker = self.cfg.requests_dir / "reboot-notify"
        try:
            data = json.loads(marker.read_text())
            chat_id = int(data["chat_id"])
            age = self.clock() - float(data["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        marker.unlink(missing_ok=True)
        if age > REBOOT_NOTIFY_MAX_AGE:
            return
        up = self.uptime()
        self._governed_done("reboot", up is not None and up < age)
        if up is not None and up < age:
            text = f"Back online after reboot (uptime {human_duration(up)})."
        else:
            text = "The bot restarted but the laptop did not reboot."
        for _ in range(6):  # the network may still be coming up
            try:
                self.api.call("sendMessage", {"chat_id": chat_id, "text": text})
                return
            except TelegramError as e:
                log.info("reboot notice not sent yet: %s", e)
                self.sleep(10)

    def _send_report(self, chat_id: int) -> None:
        name = time.strftime("edge-diagnostics-%Y%m%d-%H%M%S.txt")
        env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
        env.update(
            EDGE_SERVER_DIR=str(self.cfg.app_dir),
            FRIGATE_URL=self.cfg.frigate_url,
            HOST_SNAPSHOT=str(self.cfg.status_dir / "host-snapshot.txt"),
            HOST_SNAPSHOT_LIVE="false",  # the container sees no host firewall: use the host's file
            SAMPLES=str(self.cfg.diag_samples),
            INTERVAL=str(self.cfg.diag_interval),
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / name
            try:
                result = subprocess.run(
                    ["bash", str(self.cfg.diag_script), str(out)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                data = out.read_bytes()
            except (OSError, subprocess.TimeoutExpired) as e:
                log.error("diagnostics failed: %s", e)
                self.reply(chat_id, "Diagnostics failed (see the bot's docker logs).")
                return
        if result.returncode != 0:
            log.warning(
                "collect-diagnostics.sh exited %s: %s", result.returncode, result.stderr[-300:]
            )
        data = data.replace(self.cfg.token.encode(), b"***")
        try:
            self.api.send_document(
                chat_id,
                name,
                data,
                caption="Diagnostics. Passwords are removed; it still lists private IPs and config.",
            )
        except TelegramError as e:
            log.error("could not send report: %s", e)
            self.reply(chat_id, "Could not upload the report to Telegram.")

    # -- dispatch ---------------------------------------------------------

    def handle_message(self, message: dict) -> None:
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        sender = message.get("from") or {}
        user_id = sender.get("id")
        chat_id = (message.get("chat") or {}).get("id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        if self.clock() - float(message.get("date") or 0) > self.cfg.max_age:
            log.info("ignoring old command %r", text.split()[0])
            return
        command = text.split()[0].split("@")[0].lower()
        args = text.split()[1:]

        if command == "/id":
            self.reply(chat_id, f"Your Telegram user id: {user_id}")
            return
        if user_id not in self.cfg.allowed:
            log.warning("ignored %s from unauthorized user id %s", command, user_id)
            if command in ("/start", "/help"):
                self.reply(
                    chat_id,
                    f"Not authorized. Your user id is {user_id}: add it to "
                    "TELEGRAM_ALLOWED_IDS in .telegram on the server and redeploy.",
                )
            return

        log.info("%s from user id %s", command, user_id)
        if command == "/status":
            self.cmd_status(chat_id)
        elif command == "/diag":
            thread = threading.Thread(target=self.cmd_diag, args=(chat_id,), daemon=True)
            thread.start()
            self.threads.append(thread)
        elif command == "/fleet":
            self.cmd_fleet(chat_id)
        elif command == "/audit":
            self.cmd_audit(chat_id)
        elif command == "/farm":
            self.cmd_farm(chat_id)
        elif command == "/ask":
            thread = threading.Thread(target=self.cmd_ask, args=(chat_id, args), daemon=True)
            thread.start()
            self.threads.append(thread)
        elif command == "/pending":
            self.cmd_pending(chat_id)
        elif command == "/approve":
            self.cmd_approve(chat_id, user_id, message, args)
        elif command == "/deny":
            self.cmd_deny(chat_id, user_id, args)
        elif command in ("/suspend", "/hold"):
            self.cmd_suspend(chat_id, user_id, args, command[1:])
        elif command == "/resume":
            self.cmd_resume(chat_id, user_id, message, args)
        elif command == "/testalert":
            self.cmd_testalert(chat_id, args)
        elif command == "/reboot":
            self.cmd_reboot(chat_id, user_id, message, args)
        elif command == "/redeploy":
            self.cmd_redeploy(chat_id, user_id, message, args)
        elif command == "/restart":
            self.cmd_restart(chat_id, user_id, message, args)
        elif command == "/logs":
            self.cmd_logs(chat_id, args)
        else:
            self.reply(chat_id, self.help_text())

    def handle_update(self, update: dict) -> None:
        try:
            if isinstance(update.get("message"), dict):
                self.handle_message(update["message"])
        except Exception:  # noqa: BLE001 - one bad update must not stop the bot
            log.exception("error handling update")

    # -- polling ----------------------------------------------------------

    def poll_once(self, offset: int | None, timeout: int = 30) -> int | None:
        payload: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        updates = self.api.call("getUpdates", payload, timeout=timeout + 10)
        for update in updates if isinstance(updates, list) else []:
            offset = int(update["update_id"]) + 1
            self.handle_update(update)
        return offset

    def run(self) -> None:
        self.setup_control()
        self.setup_governance()
        self.setup_assistant()
        if self.governance is not None:
            threading.Thread(target=self.verify_loop, daemon=True).start()
        try:
            self.api.call("setMyCommands", {"commands": self.commands()})
        except TelegramError as e:
            log.warning("could not register the command menu: %s", e)
        log.info(
            "bot started; %d authorized user(s); commands: /status /diag /testalert /id /help%s",
            len(self.cfg.allowed),
            " /reboot /redeploy /restart /logs" if self.cfg.totp_secret else "",
        )
        if self.cfg.totp_secret:
            threading.Thread(target=self.notify_after_reboot, daemon=True).start()
            self.resume_redeploy()
        if not self.cfg.allowed:
            log.warning("TELEGRAM_ALLOWED_IDS is empty: send /id to the bot to get your id")
        if self.cfg.allowed and self.cfg.alert_free_percent > 0:
            threading.Thread(target=self.alert_loop, daemon=True).start()
            log.info(
                "disk alerts on: below %g%% free, every %g s",
                self.cfg.alert_free_percent,
                self.cfg.alert_interval,
            )
        if self.cfg.allowed and self.cfg.watch:
            threading.Thread(target=self.watch_loop, daemon=True).start()
            log.info(
                "watchers on every %g s (Frigate, cameras, temperature, disk health); "
                "alert after %d checks in a row",
                self.cfg.watch_interval,
                self.cfg.alert_confirm,
            )
        offset: int | None = None
        failures = 0
        while True:
            try:
                offset = self.poll_once(offset)
                failures = 0
            except TelegramError as e:
                failures += 1
                delay = min(60, 2**failures)
                log.warning("polling failed (%s); retrying in %ds", e, delay)
                self.sleep(delay)


def check_environment(cfg: Config) -> list[str]:
    """Problems that would break /diag or /status inside the container."""
    import shutil

    problems = []
    for tool in ("bash", "ps", "top", "lscpu", "lspci", "curl", "timeout", "awk"):
        if shutil.which(tool) is None:
            problems.append(f"missing tool: {tool}")
    if not cfg.diag_script.is_file():
        problems.append(f"missing {cfg.diag_script}")
    return problems


def install_redaction(cfg: Config) -> bool:
    """Mask the bot token, the authenticator secret and secret-shaped text in every log line
    (Autonomy M9, autonomy-core's redactor). False when autonomy-core is not installed."""
    try:
        import autonomy_core as core
    except ImportError:
        return False
    for value in (cfg.token, cfg.totp_secret):
        if value:
            core.REDACTOR.register(value)
    for name, value in os.environ.items():
        if any(w in name.upper() for w in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")) and value:
            core.REDACTOR.register(value)
    redacting = core.telemetry.RedactingFilter()
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, core.telemetry.RedactingFilter) for f in handler.filters):
            handler.addFilter(redacting)
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    if "--totp-setup" in argv:
        return totp_setup()
    try:
        cfg = Config.from_env()
    except ValueError as e:
        log.error("%s", e)
        return 2
    install_redaction(cfg)
    if "--check" in argv:
        problems = check_environment(cfg)
        for p in problems:
            print(p)
        print("check failed" if problems else "check ok")
        return 1 if problems else 0

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 ignores it otherwise
    Bot(cfg, Telegram(cfg.token, cfg.api_url)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
