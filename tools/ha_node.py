#!/usr/bin/env python3
"""Configure a Home Assistant OS node from this machine (the edge server as a jumpbox).

HAOS is installed from its own image; this tool only puts configuration on it. See
docs/design/ha-node.md. It reaches the node over SSH (the "Advanced SSH & Web Terminal"
add-on, key login) and the HA API (a long-lived token).

Commands:
  ha_node.py init  --out FILE ...                write the per-site file (no secrets in it)
  ha_node.py check FILE                          SSH, /config, the ha CLI and the API token
  ha_node.py plan  FILE --source DIR             what a sync would add, change and remove
  ha_node.py backup FILE                         take an HA backup and print its id
  ha_node.py sync  FILE --source DIR [--restart] [--add-include]
                                                 backup, copy, check_config, roll back on failure

Rules: only files this tool put there (listed in ``/config/.autonomy-managed.json``) are
ever replaced or removed; an existing file it does not own is a conflict and stops the
sync. ``configuration.yaml``, ``secrets.yaml`` and ``.storage`` are never touched, except
for adding the ``packages:`` include with ``--add-include`` when that is safe. Standard
library plus PyYAML; the token is read from a file and never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

CONFIG_DIR = "/config"
MANIFEST = f"{CONFIG_DIR}/.autonomy-managed.json"
ALLOWED_ROOTS = ("packages", "custom_components", "blueprints", "www")
INCLUDE_BLOCK = "homeassistant:\n  packages: !include_dir_named packages\n"


class HaNodeError(Exception):
    pass


class Node(Protocol):
    """File and command access to the node's /config (SSH in production, a fake in tests)."""

    def run(self, command: str) -> tuple[int, str]: ...

    def read(self, path: str) -> bytes | None: ...

    def write(self, path: str, data: bytes) -> None: ...

    def delete(self, path: str) -> None: ...


class Api(Protocol):
    def ping(self) -> bool: ...

    def check_config(self) -> tuple[bool, str]: ...

    def restart(self) -> None: ...


# --------------------------------------------------------------------------- transports


class SshNode:
    def __init__(self, host: str, user: str, port: int = 22, key: str = "") -> None:
        self.base = ["ssh", "-p", str(port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        self.base += ["-o", "StrictHostKeyChecking=accept-new"]
        if key:
            self.base += ["-i", key]
        self.base.append(f"{user}@{host}")

    def _ssh(self, command: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                self.base + [command], input=data, capture_output=True, timeout=120, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HaNodeError(f"ssh failed: {exc}") from exc

    def run(self, command: str) -> tuple[int, str]:
        p = self._ssh(command)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")

    def read(self, path: str) -> bytes | None:
        p = self._ssh(f"test -f {shlex.quote(path)} && cat {shlex.quote(path)}")
        if p.returncode == 0:
            return p.stdout
        if p.returncode == 1 and not p.stderr:
            return None  # the file is not there
        raise HaNodeError(f"cannot read {path}: {p.stderr.decode('utf-8', 'replace').strip()}")

    def write(self, path: str, data: bytes) -> None:
        d = str(PurePosixPath(path).parent)
        p = self._ssh(f"mkdir -p {shlex.quote(d)} && cat > {shlex.quote(path)}", data)
        if p.returncode != 0:
            raise HaNodeError(f"cannot write {path}: {p.stderr.decode('utf-8', 'replace').strip()}")

    def delete(self, path: str) -> None:
        p = self._ssh(f"rm -f {shlex.quote(path)}")
        if p.returncode != 0:
            raise HaNodeError(
                f"cannot delete {path}: {p.stderr.decode('utf-8', 'replace').strip()}"
            )


class HttpApi:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                req, timeout=60
            ) as resp:  # noqa: S310 - the owner's own HA URL
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise HaNodeError(f"HA API {path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise HaNodeError(f"HA API {path}: {exc}") from exc
        return json.loads(raw) if raw else None

    def ping(self) -> bool:
        try:
            return "running" in str(self._call("GET", "/api/").get("message", ""))
        except (HaNodeError, AttributeError):
            return False

    def check_config(self) -> tuple[bool, str]:
        out = self._call("POST", "/api/config/core/check_config") or {}
        return out.get("result") == "valid", str(out.get("errors") or "")

    def restart(self) -> None:
        self._call("POST", "/api/services/homeassistant/restart", {})


# --------------------------------------------------------------------------- site file


def load_site(path: str) -> dict[str, Any]:
    import yaml

    try:
        site = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise HaNodeError(f"{path}: {exc}") from exc
    if not isinstance(site, dict) or not str(site.get("host", "")).strip():
        raise HaNodeError(f"{path}: 'host' (the node's address) is required")
    return site


def connect(site: dict[str, Any]) -> tuple[SshNode, HttpApi]:
    host = str(site["host"])
    node = SshNode(
        host,
        str(site.get("ssh_user", "root")),
        int(site.get("ssh_port", 22)),
        str(site.get("ssh_key", "")),
    )
    token = ""
    if site.get("token_file"):
        try:
            token = Path(str(site["token_file"])).read_text().strip()
        except OSError as exc:
            raise HaNodeError(f"cannot read the token file: {exc}") from exc
    return node, HttpApi(str(site.get("url") or f"http://{host}:8123"), token)


# --------------------------------------------------------------------------- source and plan


def read_source(source: Path) -> dict[str, bytes]:
    """The files to put on the node, keyed by their path under /config."""
    if not source.is_dir():
        raise HaNodeError(f"{source}: not a directory")
    files: dict[str, bytes] = {}
    for root in ALLOWED_ROOTS:
        base = source / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_symlink():
                raise HaNodeError(f"{p}: symbolic links are not copied")
            if p.is_file() and not any(
                part.startswith(".") for part in p.relative_to(source).parts
            ):
                files[p.relative_to(source).as_posix()] = p.read_bytes()
    return files


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest(node: Node) -> dict[str, Any]:
    raw = node.read(MANIFEST)
    if raw is None:
        return {"files": {}}
    try:
        m = json.loads(raw)
        if isinstance(m, dict) and isinstance(m.get("files"), dict):
            return dict(m)
    except ValueError:
        pass
    raise HaNodeError(f"{MANIFEST} is damaged; fix or remove it by hand")


def make_plan(node: Node, wanted: dict[str, bytes]) -> dict[str, list[str]]:
    owned: dict[str, str] = load_manifest(node)["files"]
    plan: dict[str, list[str]] = {"add": [], "change": [], "same": [], "remove": [], "conflict": []}
    for rel, data in sorted(wanted.items()):
        there = node.read(f"{CONFIG_DIR}/{rel}")
        if there is None:
            plan["add"].append(rel)
        elif there == data:
            plan["same"].append(rel)
        elif rel in owned and owned[rel] == sha(there):
            plan["change"].append(rel)
        else:  # exists, is not ours or was edited by hand since: never overwritten
            plan["conflict"].append(rel)
    for rel in sorted(set(owned) - set(wanted)):
        there = node.read(f"{CONFIG_DIR}/{rel}")
        if there is not None:
            (plan["remove"] if owned[rel] == sha(there) else plan["conflict"]).append(rel)
    return plan


# --------------------------------------------------------------------------- configuration.yaml


def include_state(node: Node) -> str:
    """'present', 'missing' (safe to add) or 'manual' (a homeassistant: block exists)."""
    raw = node.read(f"{CONFIG_DIR}/configuration.yaml")
    text = (raw or b"").decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    if any(ln.strip().startswith("packages:") for ln in lines):
        return "present"
    if any(ln.rstrip() == "homeassistant:" or ln.startswith("homeassistant:") for ln in lines):
        return "manual"
    return "missing"


# --------------------------------------------------------------------------- commands


def do_check(node: Node, api: Api, out: Any = print) -> bool:
    ok = True
    rc, text = node.run("echo ok")
    out(f"ssh: {'ok' if rc == 0 and 'ok' in text else 'FAILED ' + text.strip()}")
    ok &= rc == 0
    if rc == 0:
        has_cfg = node.read(f"{CONFIG_DIR}/configuration.yaml") is not None
        out(
            f"{CONFIG_DIR}/configuration.yaml: {'found' if has_cfg else 'NOT FOUND (is this the HA add-on SSH, with /config mapped?)'}"
        )
        ok &= has_cfg
        rc2, _ = node.run("command -v ha")
        out(
            f"ha CLI (backups): {'available' if rc2 == 0 else 'NOT FOUND: install Advanced SSH & Web Terminal and turn protection mode off'}"
        )
        ok &= rc2 == 0
    api_ok = api.ping()
    out(f"HA API token: {'ok' if api_ok else 'FAILED (wrong URL or token)'}")
    ok &= api_ok
    return bool(ok)


def do_backup(node: Node) -> str:
    name = "autonomy-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    rc, text = node.run(f"ha backups new --name {shlex.quote(name)} --raw-json")
    if rc != 0:
        raise HaNodeError(f"backup failed: {text.strip()[:300]}")
    try:
        data = json.loads(text.strip().splitlines()[-1])
        slug = str(data["data"]["slug"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HaNodeError(f"backup answered something unexpected: {text.strip()[:200]}") from exc
    return slug


def do_sync(
    node: Node,
    api: Api,
    wanted: dict[str, bytes],
    restart: bool = False,
    add_include: bool = False,
    out: Any = print,
) -> bool:
    plan = make_plan(node, wanted)
    if plan["conflict"]:
        raise HaNodeError(
            "not touching files that exist and are not ours (or were edited by hand): "
            + ", ".join(plan["conflict"])
            + ". Move them away or add them to the source on purpose."
        )
    state = include_state(node)
    if state == "manual":
        out(
            "configuration.yaml has a homeassistant: block without packages:. Add this under it yourself:"
        )
        out("    packages: !include_dir_named packages")
    changes = plan["add"] + plan["change"] + plan["remove"]
    if not changes and not (add_include and state == "missing"):
        out("nothing to change")
        return True

    backup = do_backup(node)
    out(f"backup {backup} taken")

    manifest = load_manifest(node)
    saved: dict[str, bytes | None] = {}
    cfg_path = f"{CONFIG_DIR}/configuration.yaml"
    saved_cfg = node.read(cfg_path)

    def undo() -> None:
        for rel, old in saved.items():
            if old is None:
                node.delete(f"{CONFIG_DIR}/{rel}")
            else:
                node.write(f"{CONFIG_DIR}/{rel}", old)
        if saved_cfg is not None:
            node.write(cfg_path, saved_cfg)
        node.write(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True).encode())

    try:
        for rel in plan["add"] + plan["change"]:
            saved[rel] = node.read(f"{CONFIG_DIR}/{rel}")
            node.write(f"{CONFIG_DIR}/{rel}", wanted[rel])
        for rel in plan["remove"]:
            saved[rel] = node.read(f"{CONFIG_DIR}/{rel}")
            node.delete(f"{CONFIG_DIR}/{rel}")
        if add_include and state == "missing":
            base = saved_cfg or b""
            sep = b"" if base.endswith(b"\n") or not base else b"\n"
            node.write(cfg_path, base + sep + INCLUDE_BLOCK.encode())
        files = {r: sha(wanted[r]) for r in wanted}
        node.write(
            MANIFEST,
            json.dumps({"files": files, "backup": backup}, indent=1, sort_keys=True).encode(),
        )
        valid, errors = api.check_config()
    except Exception:
        undo()
        raise
    if not valid:
        undo()
        out("check_config FAILED; the previous files are back, nothing restarted")
        if errors:
            out(errors[:600])
        return False
    out(
        f"check_config ok; {len(plan['add'])} added, {len(plan['change'])} changed, {len(plan['remove'])} removed"
    )
    if restart:
        api.restart()
        out("Home Assistant is restarting")
    else:
        out("not restarted: restart Home Assistant (or run with --restart) to load it")
    return True


def do_init(args: argparse.Namespace) -> int:
    import yaml

    site = {"host": args.host, "ssh_user": args.ssh_user, "ssh_port": args.ssh_port}
    for key in ("ssh_key", "token_file", "url"):
        if getattr(args, key):
            site[key] = getattr(args, key)
    Path(args.out).write_text(
        "# Per-site file for tools/ha_node.py. No secrets: the token stays in its own file.\n"
        + yaml.safe_dump(site, sort_keys=False)
    )
    print(f"wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--out", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--ssh-user", default="root")
    p.add_argument("--ssh-port", type=int, default=22)
    p.add_argument("--ssh-key", default="")
    p.add_argument("--token-file", default="")
    p.add_argument("--url", default="")
    for name in ("check", "plan", "backup", "sync"):
        p = sub.add_parser(name)
        p.add_argument("file")
        if name in ("plan", "sync"):
            p.add_argument(
                "--source", required=True, help="directory with packages/, custom_components/, ..."
            )
        if name == "sync":
            p.add_argument("--restart", action="store_true")
            p.add_argument("--add-include", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "init":
            return do_init(args)
        node, api = connect(load_site(args.file))
        if args.cmd == "check":
            return 0 if do_check(node, api) else 1
        if args.cmd == "backup":
            print(do_backup(node))
            return 0
        wanted = read_source(Path(args.source))
        if args.cmd == "plan":
            for kind, items in make_plan(node, wanted).items():
                for rel in items:
                    print(f"{kind:9} {rel}")
            return 0
        return 0 if do_sync(node, api, wanted, args.restart, args.add_include) else 1
    except HaNodeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
