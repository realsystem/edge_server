#!/usr/bin/env python3
"""Host side of the Telegram bot's /restart and /logs commands.

Runs as root on the host, started by a systemd path unit (edge-restart.path,
edge-logs.path) when the bot drops a request file into bot-requests/. The bot's
container has no Docker access; this is the only thing it can ask for, and it
accepts nothing but a service name from the stack's own compose file:

  restart  docker compose restart <service>, then wait until it is running again
  logs     the last lines of <service>'s log, passwords removed, one timestamp per
           line in the server's time zone (Docker's, converted from UTC)

Usage: service-request.py restart|logs
Environment: EDGE_SERVER_DIR (default /opt/edge-server), FRIGATE_URL
(default http://127.0.0.1:5000)

The request is deleted before anything runs, so it can never loop. The answer
is written to bot-requests/<kind>-result (JSON); for logs the text goes to
bot-requests/logs-output.txt first.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
NEVER_RESTART = {"telegram-bot"}  # restarting the bot from the bot would cut off its own answer
DEFAULT_LINES, MAX_LINES = 100, 1000
MAX_LOG_BYTES = 1_000_000
MAX_REQUEST_BYTES = 4096
RESTART_WAIT = 120.0  # seconds to wait for the service to run again
POLL = 3.0

# Same idea as collect-diagnostics.sh: no passwords, tokens or keys in what leaves the host
URL_CREDENTIALS = re.compile(r'(://)[^\s"\']*@')
KEY_VALUE = re.compile(
    r'((pass(word)?|passwd|secret|token|api_?key)[A-Za-z0-9_]*[ ]*[=:][ ]*"?)[^ ",]+', re.I
)
BOT_TOKEN = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}")  # also in .../bot<token>/getUpdates


def app_dir() -> Path:
    return Path(os.environ.get("EDGE_SERVER_DIR", "/opt/edge-server"))


def requests_dir() -> Path:
    return app_dir() / "bot-requests"


def write_atomic(path: Path, data: bytes | str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode())
    tmp.chmod(0o644)
    os.replace(tmp, path)


def write_result(kind: str, status: str, service: str = "", detail: str = "", **extra) -> None:
    result = {"status": status, "service": service, "detail": detail, "at": time.time(), **extra}
    write_atomic(requests_dir() / f"{kind}-result", json.dumps(result))


def cam_passwords(cam_file: Path) -> set[str]:
    """Literal camera passwords from .cam, in the forms they appear in logs and configs."""
    found: set[str] = set()
    try:
        lines = cam_file.read_text(errors="replace").splitlines()
    except OSError:
        return found
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        first, _, rest = line.partition(" ")
        if re.fullmatch(r"[0-9.]+|[0-9a-fA-F:]+", first) and rest:
            line = rest.strip()
        password = line.partition(":")[2]
        if len(password) >= 4:
            found.update(
                {
                    password,
                    urllib.parse.quote(password, safe=""),
                    password.replace("{", "{{").replace("}", "}}"),
                }
            )
    return found


# "frigate  | 2026-11-01T14:49:20.442382760Z 2026-11-01 14:49:20.442301969  <message>":
# Docker's UTC timestamp (from --timestamps), then, for Frigate, a second UTC one from
# the image's log wrapper. Both are UTC, and a message often has its own local time too.
LOG_STAMPS = re.compile(
    r"^(?P<head>[^\s|]+\s*\| )?"
    r"(?P<docker>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)Z "
    r"(?P<wrapper>(?P<wrapper_time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) +)?"
)
DUPLICATE_WITHIN = 5.0  # seconds: a second timestamp this close to Docker's is the same moment


def parse_utc(text: str) -> datetime | None:
    base, _, fraction = text.replace("T", " ").partition(".")
    try:
        moment = datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
        micro = int((fraction + "000000")[:6]) if fraction else 0
    except ValueError:
        return None
    return moment.replace(microsecond=micro, tzinfo=timezone.utc)


def localize_log_line(line: str) -> str:
    """One timestamp per line, in the server's own time zone: Docker's, converted from UTC.
    The wrapper's duplicate is dropped; lines without a Docker timestamp are left alone."""
    match = LOG_STAMPS.match(line)
    moment = parse_utc(match["docker"]) if match else None
    if match is None or moment is None:
        return line
    local = moment.astimezone()
    stamp = f"{local:%Y-%m-%d %H:%M:%S}.{local.microsecond // 1000:03d} {local:%Z}"
    wrapper = match["wrapper"] or ""
    if wrapper:
        other = parse_utc(match["wrapper_time"])
        if other is not None and abs((other - moment).total_seconds()) <= DUPLICATE_WITHIN:
            wrapper = ""  # the same moment again; anything else is part of the message
    return f"{match['head'] or ''}{stamp} {wrapper}{line[match.end():]}"


def redact(text: str, passwords: set[str] | None = None) -> str:
    text = URL_CREDENTIALS.sub(r"\1***@", text)
    text = KEY_VALUE.sub(r"\1***", text)
    text = BOT_TOKEN.sub("***", text)
    for password in sorted(passwords or (), key=len, reverse=True):
        text = text.replace(password, "***")
    return text


def run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=app_dir(), capture_output=True, text=True, timeout=timeout, check=False
    )


def compose_services() -> list[str]:
    result = run(["docker", "compose", "config", "--services"], 30)
    if result.returncode != 0:
        return []
    return [s.strip() for s in result.stdout.splitlines() if s.strip()]


def read_request(kind: str) -> dict | None:
    """Consume bot-requests/<kind>-request. None if it is missing or not a JSON object."""
    path = requests_dir() / f"{kind}-request"
    try:
        raw = path.read_bytes()[: MAX_REQUEST_BYTES + 1]
    except OSError:
        return None
    finally:
        path.unlink(missing_ok=True)  # first: a bad request must not be seen again
    if len(raw) > MAX_REQUEST_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def pick_service(kind: str, request: dict | None) -> tuple[str | None, str]:
    """(service, "") if the request names a service of this stack, else (None, why)."""
    name = request.get("service") if request else None
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None, "the request did not name a valid service"
    services = compose_services()
    if kind == "restart":
        services = [s for s in services if s not in NEVER_RESTART]
    if name not in services:
        return None, f"unknown service {name!r}; known: {', '.join(services) or 'none'}"
    return name, ""


def container_state(service: str) -> tuple[str, bool]:
    """(status, restarting) of the service's container; ("missing", False) if none."""
    ids = run(["docker", "compose", "ps", "-aq", service], 30).stdout.split()
    if not ids:
        return "missing", False
    fmt = "{{.State.Status}} {{.State.Restarting}}"
    out = run(["docker", "inspect", "-f", fmt, ids[0]], 30).stdout.split()
    if len(out) != 2:
        return "missing", False
    return out[0], out[1] == "true"


def frigate_answers() -> bool:
    url = os.environ.get("FRIGATE_URL", "http://127.0.0.1:5000").rstrip("/") + "/api/version"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - fixed local URL
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def do_restart() -> int:
    service, why = pick_service("restart", read_request("restart"))
    if service is None:
        write_result("restart", "refused", detail=why)
        return 1
    try:
        result = run(["docker", "compose", "restart", "-t", "30", service], 240)
    except subprocess.TimeoutExpired:
        write_result("restart", "failed", service, "docker did not finish restarting it in time")
        return 1
    if result.returncode != 0:
        tail = (result.stderr.strip().splitlines() or ["docker compose failed"])[-1]
        write_result("restart", "failed", service, redact(tail)[:200])
        return 1

    deadline = time.monotonic() + RESTART_WAIT
    stable = 0
    while time.monotonic() < deadline:
        status, restarting = container_state(service)
        stable = stable + 1 if status == "running" and not restarting else 0
        if stable >= 2 and (service != "frigate" or frigate_answers()):
            write_result("restart", "ok", service)
            return 0
        time.sleep(POLL)
    status, restarting = container_state(service)
    if status == "running" and not restarting:
        # up, but Frigate's API is silent: it can need a minute more to load
        write_result("restart", "ok", service, "running; Frigate's API is not answering yet")
        return 0
    write_result("restart", "failed", service, f"not running after the restart (state: {status})")
    return 1


def do_logs() -> int:
    request = read_request("logs")
    service, why = pick_service("logs", request)
    if service is None:
        write_result("logs", "refused", detail=why)
        return 1
    try:
        lines = int((request or {}).get("lines", DEFAULT_LINES))
    except (TypeError, ValueError):
        lines = DEFAULT_LINES
    lines = max(1, min(lines, MAX_LINES))
    try:
        result = run(
            [
                "docker",
                "compose",
                "logs",
                "--no-color",
                "--timestamps",
                "--tail",
                str(lines),
                service,
            ],
            60,
        )
    except subprocess.TimeoutExpired:
        write_result("logs", "failed", service, "docker did not answer in time")
        return 1
    if result.returncode != 0:
        tail = (result.stderr.strip().splitlines() or ["docker compose failed"])[-1]
        write_result("logs", "failed", service, redact(tail)[:200])
        return 1
    text = redact(result.stdout + result.stderr, cam_passwords(app_dir() / ".cam"))
    text = "".join(localize_log_line(line) for line in text.splitlines(keepends=True))
    data = text.encode(errors="replace")[-MAX_LOG_BYTES:]  # keep the newest
    write_atomic(requests_dir() / "logs-output.txt", data)
    write_result("logs", "ok", service, lines=lines, bytes=len(data))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    handlers = {"restart": do_restart, "logs": do_logs}
    if len(argv) != 1 or argv[0] not in handlers:
        print("usage: service-request.py restart|logs", file=sys.stderr)
        return 2
    try:
        return handlers[argv[0]]()
    except Exception as e:  # noqa: BLE001 - the bot is waiting for an answer, whatever went wrong
        write_result(argv[0], "failed", detail=f"{type(e).__name__}: {redact(str(e))}"[:200])
        return 1


if __name__ == "__main__":
    sys.exit(main())
