#!/usr/bin/env python3
"""host-stub: the host side of the bot's drop box, for the lab (a laptop has no systemd units).

Watches $EDGE_SERVER_DIR/bot-requests like the production path units do:
  restart-request, logs-request  run the REAL telegram-bot/service-request.py (docker compose
                                 restart / logs on the lab stack)
  reboot                         SIMULATED: removes the request, restarts the bot and Frigate
                                 and resets a fake /proc/uptime (refused within 10 minutes of
                                 the last "boot", like the production unit)
  redeploy                       SIMULATED: writes a redeploy-result "ok"
It also keeps FAKEPROC/uptime moving, so the bot has an uptime to verify a reboot against.
Lab only: it touches containers of the lab project and nothing else.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP = Path(os.environ["EDGE_SERVER_DIR"])
REQ = APP / "bot-requests"
FAKEPROC = Path(os.environ.get("FAKEPROC", "/fakeproc"))
HELPER = os.environ.get("SERVICE_REQUEST", "/opt/service-request.py")
LAB_CONTAINERS = os.environ.get("REBOOT_RESTARTS", "telegram-bot frigate").split()
START_UPTIME = float(os.environ.get("START_UPTIME", str(3 * 86400)))

state = {"base": START_UPTIME, "since": time.time()}


def uptime() -> float:
    return state["base"] + (time.time() - state["since"])


def write_uptime() -> None:
    tmp = FAKEPROC / ".uptime.tmp"
    tmp.write_text(f"{uptime():.2f} 0.00\n")
    os.replace(tmp, FAKEPROC / "uptime")


def link_proc() -> None:
    """Everything but uptime points at the real /proc, resolved inside the bot container."""
    FAKEPROC.mkdir(parents=True, exist_ok=True)
    for entry in os.listdir("/proc"):
        if entry.isdigit() or entry in ("uptime", "self", "thread-self"):
            continue
        link = FAKEPROC / entry
        if not link.is_symlink():
            try:
                link.symlink_to(f"/proc/{entry}")
            except OSError:
                pass


def say(msg: str) -> None:
    print(f"host-stub: {msg}", flush=True)


def helper(kind: str) -> None:
    say(f"{kind}-request -> service-request.py")
    r = subprocess.run(
        [sys.executable, HELPER, kind], env=os.environ, capture_output=True, text=True, timeout=300
    )
    say(f"{kind} done ({r.returncode}) {(r.stderr or r.stdout).strip()[-200:]}")


def reboot() -> None:
    (REQ / "reboot").unlink(missing_ok=True)  # consumed first, like the unit
    if uptime() <= 600:
        say("reboot refused: the machine 'booted' under 10 minutes ago")
        return
    say("reboot (simulated): restarting " + " ".join(LAB_CONTAINERS))
    state.update(base=0.0, since=time.time())
    write_uptime()
    subprocess.run(
        ["docker", "restart", "-t", "10", *LAB_CONTAINERS],
        check=False,
        capture_output=True,
        timeout=120,
    )


def redeploy() -> None:
    (REQ / "redeploy").unlink(missing_ok=True)
    say("redeploy (simulated)")
    tmp = REQ / ".redeploy-result.tmp"
    tmp.write_text(
        json.dumps(
            {
                "status": "ok",
                "from": "aaaaaaa",
                "to": "bbbbbbb",
                "tag": "v0.0",
                "detail": "simulated in the lab: nothing was deployed",
                "at": int(time.time()),
            }
        )
    )
    tmp.chmod(0o644)
    os.replace(tmp, REQ / "redeploy-result")


def main() -> None:
    link_proc()
    write_uptime()
    say(f"watching {REQ}")
    while True:
        write_uptime()
        try:
            if REQ.is_dir():
                for kind in ("restart", "logs"):
                    if (REQ / f"{kind}-request").exists():
                        helper(kind)
                if (REQ / "reboot").exists():
                    reboot()
                if (REQ / "redeploy").exists():
                    redeploy()
        except Exception as e:  # noqa: BLE001 - keep watching
            say(f"error: {e}")
        time.sleep(1)


if __name__ == "__main__":
    main()
