#!/usr/bin/env python3
"""Prepare the bot for a Home Assistant OS add-on (the ``ha-addon`` packaging, edge-system.md).

Reads the add-on's options (``/data/options.json``) and writes, under ``/data/app``:
  env                 shell exports for the bot and the Supervisor helper (sourced by run.sh)
  docker-compose.yml  the names the bot may /restart and /logs: ``core`` plus the add-ons the owner listed
  bot-status/fleet.json   the fleet description, when a fleet file exists (default /share/autonomy/fleet.yaml)
and creates this node's signing key in /data on first start (its public key is printed for the fleet file).

Nothing here talks to the network. Secrets stay in /data/options.json and the env file (mode 600).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT_DIR = (
    HERE.parent / "telegram-bot"
    if (HERE.parent / "telegram-bot").is_dir()
    else HERE / "telegram-bot"
)
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
NODE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SetupError(Exception):
    pass


def read_options(path: Path) -> dict:
    try:
        opts = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SetupError(f"cannot read the add-on options {path}: {exc}") from exc
    if not isinstance(opts, dict):
        raise SetupError("the add-on options are not an object")
    return opts


def validate(opts: dict) -> dict:
    token = str(opts.get("telegram_bot_token", "")).strip()
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", token):
        raise SetupError(
            "telegram_bot_token is missing or not a bot token (123456:ABC...): make a bot for this node with @BotFather"
        )
    ids = [i for i in re.split(r"[,\s]+", str(opts.get("allowed_ids", "")).strip()) if i]
    if not ids or not all(i.isdigit() for i in ids):
        raise SetupError(
            "allowed_ids must list your numeric Telegram user id (send /id to the bot to learn it)"
        )
    node = str(opts.get("node", "")).strip()
    if node and not NODE.match(node):
        raise SetupError("node must be a lowercase name such as pi-1")
    addons = [str(a).strip() for a in opts.get("restart_addons") or []]
    for slug in addons:
        if not SLUG.match(slug):
            raise SetupError(
                f"restart_addons: '{slug}' is not an add-on slug (see the add-on's page, e.g. core_mosquitto)"
            )
    secret = re.sub(r"[\s=-]", "", str(opts.get("owner_totp_secret", ""))).upper()
    return {
        "token": token,
        "ids": ",".join(ids),
        "node": node,
        "addons": addons,
        "secret": secret,
        "peer_grants": str(opts.get("peer_grants", "")).strip(),
        "fleet_file": str(opts.get("fleet_file", "/share/autonomy/fleet.yaml")),
    }


def ensure_key(path: Path) -> str:
    """Create the node's Ed25519 key on first start; return its public key (base64)."""
    sys.path.insert(0, str(BOT_DIR))
    import controlplane as cp

    if not path.is_file():
        ed25519, serialization = cp._crypto()
        key = ed25519.Ed25519PrivateKey.generate()
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        tmp.replace(path)
    return str(cp.public_b64(cp.load_private_key(path)))


def setup(options: Path, data: Path, run_fleet=subprocess.run) -> list[str]:
    """Write everything; return lines for the add-on log."""
    v = validate(read_options(options))
    app = data / "app"
    for sub in ("bot-requests", "bot-status", "control"):
        (app / sub).mkdir(parents=True, exist_ok=True)
    (app / "bot-requests").chmod(0o700)
    services = ["core", *[a for a in v["addons"] if a != "core"]]
    (app / "docker-compose.yml").write_text(
        "# Written by addon-setup.py: the names the bot may restart and read logs of.\nservices:\n"
        + "".join(f"  {s}:\n    image: x\n" for s in services)
    )
    log = [f"services the bot may restart: {', '.join(services)}"]
    env = {
        "TELEGRAM_BOT_TOKEN": v["token"],
        "TELEGRAM_ALLOWED_IDS": v["ids"],
        "OWNER_TOTP_SECRET": v["secret"],
        "EDGE_NODE": v["node"],
        "PEER_GRANTS": v["peer_grants"],
        "FRIGATE_URL": "",  # a Home Assistant node has no Frigate
        "HA_HOST": "homeassistant",
        "STORAGE_PATH": str(data),
        "EDGE_SERVER_DIR": str(app),
        "APP_DIR": str(app),
        "REQUESTS_DIR": str(app / "bot-requests"),
        "STATUS_DIR": str(app / "bot-status"),
        "CONTROL_KEY": str(data / "node.key"),
        "CONTROL_STATE": str(app / "control"),
        "SERVICES": " ".join(services),
    }
    if not v["secret"]:
        log.append("owner_totp_secret is empty: /reboot, /restart and peer commands stay off")
    fleet_file = Path(v["fleet_file"])
    if v["node"] and fleet_file.is_file():
        pub = ensure_key(data / "node.key")
        log.append(f"this node's public key (put it in the fleet file as public_key): {pub}")
        done = run_fleet(
            [
                sys.executable,
                str(
                    HERE.parent / "tools" / "fleet.py"
                    if (HERE.parent / "tools").is_dir()
                    else HERE / "tools" / "fleet.py"
                ),
                "bot-config",
                str(fleet_file),
                "--node",
                v["node"],
                "--out",
                str(app / "bot-status" / "fleet.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            raise SetupError(
                "the fleet file was refused: " + (done.stdout + done.stderr).strip()[:400]
            )
        log.append(f"fleet {fleet_file}: this node is '{v['node']}'")
    else:
        log.append("no fleet file (or no node name): this node runs alone")
    out = data / "app" / "env"
    tmp = out.with_name("env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k, val in env.items():
            f.write(f"export {k}={shlex.quote(val)}\n")
    tmp.replace(out)
    return log


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    options = Path(argv[0]) if argv else Path("/data/options.json")
    data = Path(argv[1]) if len(argv) > 1 else Path("/data")
    try:
        for line in setup(options, data):
            print(f"autonomy-node: {line}", flush=True)
    except SetupError as exc:
        print(f"autonomy-node: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
