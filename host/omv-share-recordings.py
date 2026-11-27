#!/usr/bin/env python3
"""Publish Frigate's recordings folder on OpenMediaVault: a shared folder and a read-only NFS share.

Run by deploy-edge-server.sh when FRIGATE_SHARE=true on OpenMediaVault; by hand:

    omv-share-recordings.py --path /srv/dev-disk-by-label-main/frigate --client 10.0.0.0/24

It does what the web UI does (Storage > Shared Folders, Services > NFS), through omv-rpc, so OMV
keeps owning its files (/etc/exports, /etc/fstab). Safe to run again: an existing shared folder
or NFS share is reused, never duplicated, and nothing is ever removed. The changes are applied
(what the UI's "Apply" button does) only when OMV had no other changes waiting, so settings that
someone is still reviewing in the UI are not applied behind their back.

Python 3.7 compatible (OpenMediaVault 5 is Debian 10).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable  # noqa: UP035

NEW_UUID = "fa4b1c66-ef79-11e5-87a0-0002b3a176b4"  # OMV's marker for "create a new object"
DIRTY_FILE = os.environ.get("OMV_DIRTY_FILE", "/var/lib/openmediavault/dirtymodules.json")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
CLIENT_RE = re.compile(r"^([0-9]{1,3}\.){3}[0-9]{1,3}(/([0-9]|[12][0-9]|3[0-2]))?$")

Runner = Callable[[list], str]


def run(cmd: list) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd[:4])} failed: {(out.stderr or out.stdout).strip()[:300]}"
        )
    return out.stdout


def read_conf(runner: Runner, objid: str) -> Any:
    return json.loads(runner(["omv-confdbadm", "read", objid]) or "null")


def rpc(runner: Runner, service: str, method: str, params: dict) -> Any:
    out = runner(["omv-rpc", "-u", "admin", service, method, json.dumps(params)])
    data = json.loads(out) if out.strip() else None
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"{service} {method}: {str(data['error'].get('message'))[:300]}")
    return data


def find_filesystem(mounts: list, path: str) -> tuple:
    """The OMV file system holding PATH: (mntent uuid, mount dir). Bind mounts do not count."""
    best = None
    for m in mounts:
        d = str(m.get("dir", "")).rstrip("/")
        if not d or not str(m.get("fsname", "")).startswith("/dev/"):
            continue
        if path == d or path.startswith(d + "/"):
            if best is None or len(d) > len(best[1]):
                best = (m["uuid"], d)
    if best is None:
        raise RuntimeError(f"{path} is not on a file system that OpenMediaVault mounts")
    return best


def dirty_modules() -> list:
    try:
        with open(DIRTY_FILE) as f:
            return list(json.load(f))
    except (OSError, ValueError):
        return []


def publish(path: str, client: str, name: str, runner: Runner | None = None) -> dict:
    """Create what is missing; returns {"shared_folder": ..., "nfs": ..., "applied": bool, "notes": [...]}."""
    runner = runner or run
    path = path.rstrip("/")
    if not path.startswith("/"):
        raise RuntimeError("--path must be absolute")
    if not NAME_RE.match(name):
        raise RuntimeError(f"share name '{name}' must be letters, digits, - or _")
    if not CLIENT_RE.match(client):
        raise RuntimeError(
            f"--client '{client}' must be an IPv4 address or network like 10.0.0.0/24"
        )

    result = {"shared_folder": "reused", "nfs": "reused", "applied": False, "notes": []}
    was_dirty = bool(dirty_modules())

    mntent, mount_dir = find_filesystem(
        read_conf(runner, "conf.system.filesystem.mountpoint") or [], path
    )
    reldir = path[len(mount_dir) :].strip("/") + "/"
    if reldir == "/":
        raise RuntimeError("share a folder inside the file system, not its root")

    shared = [
        s
        for s in read_conf(runner, "conf.system.sharedfolder") or []
        if s.get("mntentref") == mntent
        and str(s.get("reldirpath", "")).rstrip("/") == reldir.rstrip("/")
    ]
    if shared:
        sf_uuid = shared[0]["uuid"]
        result["shared_folder"] = f"reused ({shared[0].get('name')})"
        name = str(shared[0].get("name"))
    else:
        taken = [
            s for s in read_conf(runner, "conf.system.sharedfolder") or [] if s.get("name") == name
        ]
        if taken:
            raise RuntimeError(f"a shared folder named '{name}' already points somewhere else")
        created = rpc(
            runner,
            "ShareMgmt",
            "set",
            {
                "uuid": NEW_UUID,
                "name": name,
                "mntentref": mntent,
                "reldirpath": reldir,
                "comment": "Frigate NVR recordings",
                "mode": "775",
            },
        )
        sf_uuid = created["uuid"]
        result["shared_folder"] = f"created ({name})"

    nfs = read_conf(runner, "conf.service.nfs") or {}
    existing = [
        s for s in (nfs.get("shares") or {}).get("share", []) if s.get("sharedfolderref") == sf_uuid
    ]
    if existing:
        result["nfs"] = f"reused ({existing[0].get('client')} {existing[0].get('options')})"
    else:
        rpc(
            runner,
            "Nfs",
            "setShare",
            {
                "uuid": NEW_UUID,
                "sharedfolderref": sf_uuid,
                "mntentref": NEW_UUID,
                "client": client,
                "options": "ro",
                "extraoptions": "subtree_check,insecure",
                "comment": "Frigate recordings (read-only)",
            },
        )
        result["nfs"] = f"created ({client}, read-only)"
        if not nfs.get("enable", True):
            result["notes"].append(
                "the NFS service is switched off in OMV: enable it under Services > NFS"
            )

    mods = dirty_modules()
    if not mods:
        return result
    if was_dirty:
        result["notes"].append(
            "OMV already had other changes waiting; open the web UI and press Apply so these take effect"
        )
        return result
    rpc(runner, "Config", "applyChanges", {"modules": mods, "force": False})
    result["applied"] = True
    return result


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--path", required=True, help="the folder to publish (inside an OMV file system)"
    )
    ap.add_argument("--client", required=True, help="who may mount it, e.g. 10.0.0.0/24")
    ap.add_argument("--name", default="frigate", help="shared folder name (also the /export/NAME)")
    args = ap.parse_args(argv)
    try:
        res = publish(args.path, args.client, args.name)
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        print(f"omv-share-recordings: {exc}", file=sys.stderr)
        return 1
    print(f"shared folder: {res['shared_folder']}")
    print(f"NFS share:     {res['nfs']}")
    print(f"applied:       {'yes' if res['applied'] else 'no'}")
    for note in res["notes"]:
        print(f"note:          {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
