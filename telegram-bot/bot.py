#!/usr/bin/env python3
"""Telegram bot for the edge server (standard library only).

Commands, for the users listed in TELEGRAM_ALLOWED_IDS:
  /status  quick health check: load, memory, disk, temperature, power, Frigate
  /diag    full diagnostics report (collect-diagnostics.sh), sent as a file
  /testalert [percent]  send a simulated low-disk alert to you (to test delivery)
  /help    list the commands

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
  ALERT_DISK_FREE_PERCENT, ALERT_INTERVAL, ALERT_REPEAT_HOURS  see above
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not TOKEN_RE.match(token):
            raise ValueError("TELEGRAM_BOT_TOKEN is missing or not a bot token (123456:ABC...)")
        allowed: set[int] = set()
        for item in re.split(r"[,\s]+", env.get("TELEGRAM_ALLOWED_IDS", "").strip()):
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
        return cls(
            token=token,
            allowed=frozenset(allowed),
            frigate_url=env.get("FRIGATE_URL", cls.frigate_url).rstrip("/"),
            app_dir=Path(env.get("EDGE_SERVER_DIR", str(cls.app_dir))),
            storage_path=Path(env.get("STORAGE_PATH", str(cls.storage_path))),
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
        )


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


RECOVERY_MARGIN = 3.0  # percentage points above the alert level before "recovered"


def disk_usage(path: Path) -> tuple[int, int] | None:
    """(total, free) bytes for the filesystem holding ``path``."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def watched_disks(cfg: Config) -> list[tuple[str, Path]]:
    """Disks to watch, one entry per filesystem (recordings often share the system disk)."""
    groups: dict[int, tuple[list[str], Path]] = {}
    for label, path in (("recordings", cfg.storage_path), ("system disk", cfg.app_dir)):
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

    for label, path in (("recordings", cfg.storage_path), ("system disk", cfg.app_dir)):
        line, low = disk_line(label, path)
        host.append(line)
        if low:
            problems.append(f"{label} almost full")

    frigate, frigate_problems = frigate_status(cfg)
    problems += frigate_problems

    head = "OK" if not problems else f"{len(problems)} problem(s)"
    out = [f"{'✅' if not problems else '⚠️'} Edge server: {head}"]
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
class Bot:
    cfg: Config
    api: Telegram
    sleep: Callable[[float], None] = time.sleep
    _diag_lock: threading.Lock = field(default_factory=threading.Lock)
    _diag_last: float = 0.0
    threads: list[threading.Thread] = field(default_factory=list)
    _disk_state: dict[str, DiskState] = field(default_factory=dict)
    stop: threading.Event = field(default_factory=threading.Event)

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

    def send_alerts(self) -> None:
        for text in self.disk_alert_messages():
            for chat_id in sorted(self.cfg.allowed):  # private chats: chat id == user id
                self.reply(chat_id, text)

    def alert_loop(self) -> None:
        while True:
            try:
                self.send_alerts()
            except Exception:  # noqa: BLE001 - keep watching
                log.exception("disk check failed")
            if self.stop.wait(self.cfg.alert_interval):
                return

    def _send_report(self, chat_id: int) -> None:
        name = time.strftime("edge-diagnostics-%Y%m%d-%H%M%S.txt")
        env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
        env.update(
            EDGE_SERVER_DIR=str(self.cfg.app_dir),
            FRIGATE_URL=self.cfg.frigate_url,
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
        if time.time() - float(message.get("date") or 0) > self.cfg.max_age:
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
        elif command == "/testalert":
            self.cmd_testalert(chat_id, args)
        else:
            self.reply(chat_id, HELP)

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
        try:
            self.api.call("setMyCommands", {"commands": COMMANDS})
        except TelegramError as e:
            log.warning("could not register the command menu: %s", e)
        log.info(
            "bot started; %d authorized user(s); commands: /status /diag /testalert /id /help",
            len(self.cfg.allowed),
        )
        if not self.cfg.allowed:
            log.warning("TELEGRAM_ALLOWED_IDS is empty: send /id to the bot to get your id")
        if self.cfg.allowed and self.cfg.alert_free_percent > 0:
            threading.Thread(target=self.alert_loop, daemon=True).start()
            log.info(
                "disk alerts on: below %g%% free, every %g s",
                self.cfg.alert_free_percent,
                self.cfg.alert_interval,
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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    try:
        cfg = Config.from_env()
    except ValueError as e:
        log.error("%s", e)
        return 2
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
