#!/usr/bin/env python3
"""Lab setup for two nodes (an edge node and a Home Assistant add-on node): keys, fleet file, options.

Run in the bot image (it has cryptography and PyYAML), with the edge_server checkout at /repo:
  setup.py --state DIR --edge-ip IP --pi-ip IP --edge-token T --pi-token T --pi-totp S --operator ID

Writes (DIR is the lab's edge_server state dir):
  app/node.key, app/bot-status/fleet.json    the edge node's key and fleet description (as the deploy script does)
  pi/data/node.key, pi/data/options.json     the add-on's data dir: its key and options
  pi/share/autonomy/fleet.yaml               the fleet file the add-on reads from /share
Keys are made once and kept, so a lab restart keeps the same node identities.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path


def make_key(path: Path) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    if not path.is_file():
        key = ed25519.Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        path.chmod(0o644)  # lab only: the bot runs as another user and reads it
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def fleet_yaml(
    edge_ip: str, pi_ip: str, edge_pub: str, pi_pub: str, edge_id: int, pi_id: int, operator: int
) -> str:
    return f"""schema_version: 1
fleet:
  name: lab
  telegram:
    group_id: -1000000000000
    operators: [{operator}]
    bot_to_bot: false
  alert_order: [edge-1, pi-1]
sites:
  lab:
    timezone: UTC
nodes:
  edge-1:
    site: lab
    packaging: host
    roles: [nvr]
    bot_id: {edge_id}
    lan: {edge_ip}
    public_key: {edge_pub}
    nvr:
      storage: {{uuid: lab, mount: /mnt/storage}}
  pi-1:
    site: lab
    packaging: ha-addon
    roles: [ha]
    bot_id: {pi_id}
    lan: {pi_ip}
    public_key: {pi_pub}
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--repo", default="/repo")
    ap.add_argument("--edge-ip", required=True)
    ap.add_argument("--pi-ip", required=True)
    ap.add_argument("--edge-token", required=True)
    ap.add_argument("--pi-token", required=True)
    ap.add_argument("--pi-totp", required=True)
    ap.add_argument("--operator", type=int, default=1001)
    args = ap.parse_args(argv)
    state = Path(args.state)
    edge_pub = make_key(state / "app" / "node.key")
    pi_pub = make_key(state / "pi" / "data" / "node.key")
    share = state / "pi" / "share" / "autonomy"
    share.mkdir(parents=True, exist_ok=True)
    text = fleet_yaml(
        args.edge_ip,
        args.pi_ip,
        edge_pub,
        pi_pub,
        int(args.edge_token.split(":")[0]),
        int(args.pi_token.split(":")[0]),
        args.operator,
    )
    (share / "fleet.yaml").write_text(text)
    status = state / "app" / "bot-status"
    status.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [
            sys.executable,
            f"{args.repo}/tools/fleet.py",
            "bot-config",
            str(share / "fleet.yaml"),
            "--node",
            "edge-1",
            "--out",
            str(status / "fleet.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        print((done.stdout + done.stderr).strip(), file=sys.stderr)
        return 1
    (status / "fleet.json").chmod(0o644)
    options = {
        "telegram_bot_token": args.pi_token,
        "allowed_ids": str(args.operator),
        "owner_totp_secret": args.pi_totp,
        "node": "pi-1",
        "restart_addons": ["core_mosquitto"],
        "peer_grants": "edge-1:restart=core,reboot",
        "fleet_file": "/share/autonomy/fleet.yaml",
    }
    data = state / "pi" / "data"
    (data / "options.json").write_text(json.dumps(options, indent=2))
    os.chmod(data, 0o777)
    print(f"setup: edge-1 {edge_pub[:8]}... pi-1 {pi_pub[:8]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
