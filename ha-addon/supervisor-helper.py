#!/usr/bin/env python3
"""The host helper of an ``ha-addon`` node: the bot's /restart, /logs and /reboot on Home Assistant OS.

On a Linux host the bot drops a file in ``bot-requests/`` and a systemd unit does the work
(``telegram-bot/service-request.py``). A HAOS add-on has no Docker and no systemd, so this loop plays
the same part with the Supervisor API, with the same files, the same refusals and the same answers:

  restart-request  {"service": name}  ->  restart-result    core, or an add-on slug from SERVICES
  logs-request     {"service","lines"} -> logs-output.txt + logs-result (passwords removed)
  reboot           (empty file)       ->  POST /host/reboot

It accepts nothing but names from SERVICES (set by the add-on from its options), never follows a
symbolic link, consumes each request before acting so it cannot loop, and redacts logs with the
host helper's own ``redact``. The bot never sees the Supervisor token.

Environment: EDGE_SERVER_DIR (the add-on keeps its state in /data/app), SERVICES (space-separated),
SUPERVISOR_TOKEN (set by Home Assistant), SUPERVISOR_URL (default http://supervisor).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RESTART_WAIT = 180.0
POLL = 3.0
LOOP = 1.0
DEFAULT_LINES, MAX_LINES, MAX_LOG_BYTES = 100, 1000, 1_000_000


def load_host_helper() -> Any:
    """The host helper (service-request.py) for its request reading, redaction and result files."""
    for candidate in (
        HERE / "service-request.py",
        HERE.parent / "telegram-bot" / "service-request.py",
    ):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("service_request", candidate)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError("service-request.py not found next to this helper")


class Supervisor:
    """The few Supervisor calls this helper makes. Tests replace it."""

    def __init__(self, token: str, url: str = "http://supervisor") -> None:
        self.token, self.url = token, url.rstrip("/")

    def call(self, method: str, path: str, timeout: float = 30.0) -> tuple[int, str]:
        req = urllib.request.Request(
            self.url + path, method=method, data=b"" if method == "POST" else None
        )
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(
                req, timeout=timeout
            ) as resp:  # noqa: S310 - the Supervisor
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            raise OSError(f"the Supervisor did not answer: {exc}") from exc


def restart_path(service: str) -> str:
    return "/core/restart" if service == "core" else f"/addons/{service}/restart"


def logs_path(service: str) -> str:
    return "/core/logs" if service == "core" else f"/addons/{service}/logs"


def running(sup: Supervisor, service: str) -> bool:
    """Is it up again? Core answers its API through the Supervisor proxy; an add-on reports its state."""
    try:
        if service == "core":
            status, text = sup.call("GET", "/core/api/", 10)
            return status == 200 and "running" in text
        status, text = sup.call("GET", f"/addons/{service}/info", 10)
        return status == 200 and json.loads(text).get("data", {}).get("state") == "started"
    except (OSError, ValueError, AttributeError):
        return False


class Helper:
    def __init__(
        self,
        host: Any,
        sup: Supervisor,
        services: list[str],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host, self.sup, self.services = host, sup, services
        self.sleep, self.clock = sleep, clock

    def pick(self, request: dict | None) -> tuple[str | None, str]:
        name = request.get("service") if request else None
        if not isinstance(name, str) or not self.host.NAME_RE.match(name):
            return None, "the request did not name a valid service"
        if name not in self.services:
            return None, f"unknown service {name!r}; known: {', '.join(self.services) or 'none'}"
        return name, ""

    def restart(self) -> None:
        service, why = self.pick(self.host.read_request("restart"))
        if service is None:
            self.host.write_result("restart", "refused", detail=why)
            return
        try:
            status, text = self.sup.call("POST", restart_path(service), 60)
        except OSError as exc:
            self.host.write_result("restart", "failed", service, str(exc)[:200])
            return
        if status != 200:
            self.host.write_result(
                "restart", "failed", service, f"the Supervisor answered {status}"
            )
            return
        deadline = self.clock() + RESTART_WAIT
        self.sleep(POLL)  # a restart takes the service down first: do not read the old "up"
        while self.clock() < deadline:
            if running(self.sup, service):
                self.host.write_result("restart", "ok", service)
                return
            self.sleep(POLL)
        self.host.write_result("restart", "failed", service, "it did not come back in time")

    def logs(self) -> None:
        request = self.host.read_request("logs")
        service, why = self.pick(request)
        if service is None:
            self.host.write_result("logs", "refused", detail=why)
            return
        try:
            lines = int((request or {}).get("lines", DEFAULT_LINES))
        except (TypeError, ValueError):
            lines = DEFAULT_LINES
        lines = max(1, min(lines, MAX_LINES))
        try:
            status, text = self.sup.call("GET", logs_path(service), 60)
        except OSError as exc:
            self.host.write_result("logs", "failed", service, str(exc)[:200])
            return
        if status != 200:
            self.host.write_result("logs", "failed", service, f"the Supervisor answered {status}")
            return
        tail = "".join(text.splitlines(keepends=True)[-lines:])
        data = self.host.redact(tail).encode(errors="replace")[-MAX_LOG_BYTES:]
        self.host.write_atomic(self.host.requests_dir() / "logs-output.txt", data)
        self.host.write_result("logs", "ok", service, lines=lines, bytes=len(data))

    def reboot(self) -> None:
        marker = self.host.requests_dir() / "reboot"
        self.host.remove_request(marker)  # first: the request must not be seen twice
        try:
            self.sup.call("POST", "/host/reboot", 30)
        except OSError:
            pass  # the bot's own timer tells the owner it did not happen

    def once(self) -> None:
        req = self.host.requests_dir()
        if (req / "restart-request").exists():
            self.restart()
        if (req / "logs-request").exists():
            self.logs()
        if (req / "reboot").exists():
            self.reboot()

    def run(self) -> None:
        while True:
            try:
                self.once()
            except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad request
                print(
                    f"supervisor-helper: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True
                )
            self.sleep(LOOP)


def main() -> int:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        print(
            "supervisor-helper: SUPERVISOR_TOKEN is not set (is this a Home Assistant add-on?)",
            file=sys.stderr,
        )
        return 1
    services = os.environ.get("SERVICES", "core").split()
    Helper(
        load_host_helper(),
        Supervisor(token, os.environ.get("SUPERVISOR_URL", "http://supervisor")),
        services,
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
