#!/usr/bin/env python3
"""Write disk health to a JSON file for the Telegram bot.

Runs on the host (as root, from a systemd timer), because reading SMART data
needs raw device access that the bot's locked-down container does not have.
The bot mounts the output directory read-only and raises the alerts.

Usage: smart-report.py [OUTPUT_FILE]    (default /opt/edge-server/bot-status/disks.json)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

DEFAULT_OUT = "/opt/edge-server/bot-status/disks.json"

# SATA SSD attributes whose normalized value counts down from 100 as the drive wears out
WEAR_ATTRIBUTES = {177, 202, 231, 233}


def run_smartctl(args: list[str]) -> dict | None:
    """smartctl's exit status is a bit mask of findings, so only the JSON matters."""
    try:
        result = subprocess.run(
            ["smartctl", "-j", *args], capture_output=True, text=True, timeout=120, check=False
        )
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return data if isinstance(data, dict) else None


def parse_disk(data: dict) -> dict | None:
    """The few facts the bot alerts on. None if the drive has no SMART support."""
    support = data.get("smart_support")
    if isinstance(support, dict) and support.get("available") is False:
        return None
    device = data.get("device") or {}
    name = str(device.get("name", "?")).removeprefix("/dev/")
    status = data.get("smart_status")
    disk: dict = {
        "device": name,
        "model": data.get("model_name") or data.get("device", {}).get("name") or name,
        "passed": status.get("passed") if isinstance(status, dict) else None,
        "wear_used": None,
        "reallocated": 0,
        "pending": 0,
        "media_errors": 0,
        "temperature": (data.get("temperature") or {}).get("current"),
        "hours": (data.get("power_on_time") or {}).get("hours"),
    }
    nvme = data.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        disk["wear_used"] = nvme.get("percentage_used")
        disk["media_errors"] = nvme.get("media_errors", 0)
        if nvme.get("critical_warning"):
            disk["passed"] = False
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    for attribute in table:
        ident = attribute.get("id")
        raw = (attribute.get("raw") or {}).get("value") or 0
        if ident == 5:
            disk["reallocated"] = raw
        elif ident in (197, 198):
            disk["pending"] = max(disk["pending"], raw)
        elif ident in WEAR_ATTRIBUTES and disk["wear_used"] is None:
            value = attribute.get("value")
            if isinstance(value, int) and 0 <= value <= 100:
                disk["wear_used"] = 100 - value
    return disk


def collect(runner: Callable[[list[str]], dict | None] = run_smartctl) -> dict:
    scan = runner(["--scan"])
    if scan is None:
        return {
            "generated": time.time(),
            "error": "smartctl is not installed or failed",
            "disks": [],
        }
    disks = []
    for entry in scan.get("devices") or []:
        args = ["-a", entry.get("name", "")]
        if entry.get("type"):
            args += ["-d", entry["type"]]
        data = runner(args)
        disk = parse_disk(data) if data else None
        if disk:
            disks.append(disk)
    return {"generated": time.time(), "disks": disks}


def write_atomic(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=1))
    tmp.chmod(0o644)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = Path(argv[0] if argv else DEFAULT_OUT)
    report = collect()
    write_atomic(out, report)
    if "error" in report:
        print(report["error"], file=sys.stderr)
        return 1
    print(f"wrote {out}: {len(report['disks'])} disk(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
