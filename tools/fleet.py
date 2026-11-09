#!/usr/bin/env python3
"""Read, check and apply the fleet file (``fleet.yaml``).

One small YAML file describes a fleet: its sites, its nodes with their roles, and the
Telegram operator group. Every node holds the same file and is told which entry is
itself (``--node``). The deploy script uses this tool; see docs/design/edge-system.md.

Commands:
  fleet.py check FILE [--node NAME] [--role LIST]   validate; with --node, print the plan
  fleet.py env   FILE  --node NAME [--role LIST]    shell assignments for the deploy script
  fleet.py write --node NAME --out FILE [...]       one-node file from the current install

``env`` prints ``[[ -v NAME ]] || NAME=value`` lines: a variable that is already set
(environment, command line) wins over the file, which is the deploy script's
precedence. Needs PyYAML (``python3-yaml`` on Ubuntu). Nothing here touches the system.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1  # the newest fleet file format this release understands
ROLES = ("nvr", "power", "archive", "ha", "agent")  # "base" is always on
IMPLEMENTED_ROLES = ("nvr", "power", "ha")  # archive and agent arrive in later phases
PACKAGINGS = ("host", "ha-addon")
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
UPSTREAM_REPO = "https://github.com/realsystem/edge_server.git"
DEFAULT_CONTROL_PORT = 7443  # fleet.control_port; TCP, Tailscale and site LAN only

# --- the known keys at every level. An unknown key is an error, never silently skipped.
TOP_KEYS = {"schema_version", "fleet", "sites", "nodes"}
# An Autonomy registry (realsystem/autonomy spec, registry schema) is a fleet file with
# owner keys, components and revocations added, signed by the owner. The node's bot
# verifies the signature (autonomy-core); this tool checks the structure.
REGISTRY_KEYS = {"registry_format", "owners", "components", "revoked"}
REGISTRY_FORMAT = 1
FLEET_KEYS = {
    "name",
    "telegram",
    "alert_order",
    "update",
    "suspect_after",
    "control_port",
    "agent",
}
TELEGRAM_KEYS = {"group_id", "operators", "bot_to_bot"}
AGENT_KEYS = {"turns", "minutes", "tokens"}
SITE_KEYS = {"timezone", "lan_subnet", "power", "relays"}
POWER_STAGES = ("conserve", "shed", "survive", "dark")
RELAY_KEYS = {"name", "url", "powers", "owner"}
NODE_KEYS = {
    "site",
    "packaging",
    "roles",
    "options",
    "bot_id",
    "tailscale",
    "lan",
    "public_key",
    "nvr",
    "power",
    "archive",
    "ha",
}
OPTION_KEYS = {"mqtt", "hwaccel"}
NVR_KEYS = {"storage", "archive_target", "dump_time"}
STORAGE_KEYS = {"uuid", "mount"}
NODE_POWER_KEYS = {"victron", "renogy"}
ARCHIVE_KEYS = {"arrays", "samba", "retention_days"}
SAMBA_KEYS = {"share", "user"}
RETENTION_KEYS = {"events", "configs"}


class FleetError(Exception):
    """The file is unusable: ``errors`` holds ``(line, message)`` pairs."""

    def __init__(self, errors: list[tuple[int, str]]):
        super().__init__(f"{len(errors)} error(s)")
        self.errors = errors


def _yaml() -> Any:
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML is missing (Ubuntu: sudo apt-get install python3-yaml)") from None
    return yaml


class Lines:
    """Line numbers of every key and list item, by path."""

    def __init__(self) -> None:
        self.by_path: dict[tuple, int] = {}

    def of(self, path: tuple) -> int:
        while path and path not in self.by_path:
            path = path[:-1]
        return self.by_path.get(path, 1)


def load_text(text: str) -> tuple[Any, Lines]:
    """Parse YAML, rejecting duplicate keys and remembering line numbers."""
    yaml = _yaml()
    lines = Lines()
    errors: list[tuple[int, str]] = []
    loader = yaml.SafeLoader(text)
    try:
        root = loader.get_single_node()
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise FleetError([((mark.line + 1) if mark else 1, f"not valid YAML: {exc}")]) from None
    if root is None:
        raise FleetError([(1, "the file is empty")])

    def build(node: Any, path: tuple) -> Any:
        if isinstance(node, yaml.MappingNode):
            result: dict = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=True)
                where = path + (key,)
                line = key_node.start_mark.line + 1
                if key in result:
                    errors.append((line, f"duplicate key '{key}'"))
                    continue
                lines.by_path[where] = line
                result[key] = build(value_node, where)
            return result
        if isinstance(node, yaml.SequenceNode):
            items = []
            for index, item in enumerate(node.value):
                lines.by_path[path + (index,)] = item.start_mark.line + 1
                items.append(build(item, path + (index,)))
            return items
        return loader.construct_object(node, deep=True)

    try:
        data = build(root, ())
    except yaml.YAMLError as exc:
        raise FleetError([(1, f"not valid YAML: {exc}")]) from None
    if errors:
        raise FleetError(errors)
    return data, lines


def _tz_ok(name: str) -> bool:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(name)
        return True
    except Exception:
        try:
            ZoneInfo("UTC")
        except Exception:
            return True  # no time zone database on this machine: cannot judge
        return False


def _is_int(value: Any, low: int | None = None, high: int | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return (low is None or value >= low) and (high is None or value <= high)


class Validator:
    def __init__(self, data: Any, lines: Lines):
        self.data = data
        self.lines = lines
        self.errors: list[tuple[int, str]] = []
        self.warnings: list[tuple[int, str]] = []

    def err(self, path: tuple, message: str) -> None:
        self.errors.append((self.lines.of(path), message))

    def warn(self, path: tuple, message: str) -> None:
        self.warnings.append((self.lines.of(path), message))

    def mapping(self, path: tuple, value: Any, known: set[str], what: str) -> dict | None:
        if not isinstance(value, dict):
            self.err(path, f"{what} must be a mapping")
            return None
        for key in value:
            if key not in known:
                self.err(path + (key,), f"unknown setting '{key}' in {what}")
        return value

    def need(self, path: tuple, value: Any, kind: type | tuple, what: str) -> bool:
        if isinstance(value, bool) and kind is not bool:
            self.err(path, f"{what} must not be true/false")
            return False
        if not isinstance(value, kind):
            names = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
            self.err(path, f"{what} must be a {names}")
            return False
        return True

    def run(self) -> None:
        top = self.mapping((), self.data, TOP_KEYS | REGISTRY_KEYS, "the file")
        if top is None:
            return
        version = top.get("schema_version")
        if version is None:
            self.err((), f"schema_version is missing (this release understands {SCHEMA_VERSION})")
        elif not _is_int(version, 1):
            self.err(("schema_version",), "schema_version must be a whole number")
        elif version > SCHEMA_VERSION:
            self.err(
                ("schema_version",),
                f"schema_version {version} is newer than this release understands "
                f"({SCHEMA_VERSION}); update edge_server first",
            )
        sites = self.check_sites(top.get("sites"))
        nodes = self.check_nodes(top.get("nodes"), sites)
        self.check_fleet(top.get("fleet"), nodes)
        self.check_cross(nodes, sites)

    # ---- fleet
    def check_fleet(self, fleet: Any, nodes: dict) -> None:
        path = ("fleet",)
        if fleet is None:
            self.err((), "fleet is missing")
            return
        fleet = self.mapping(path, fleet, FLEET_KEYS, "fleet")
        if fleet is None:
            return
        if "name" in fleet:
            self.need(path + ("name",), fleet["name"], str, "fleet.name")
        telegram = fleet.get("telegram")
        if telegram is None:
            self.err(path, "fleet.telegram is missing")
        else:
            telegram = self.mapping(path + ("telegram",), telegram, TELEGRAM_KEYS, "fleet.telegram")
            if telegram is not None:
                if "group_id" in telegram and not _is_int(telegram["group_id"]):
                    self.err(path + ("telegram", "group_id"), "group_id must be a whole number")
                ops = telegram.get("operators")
                if not isinstance(ops, list) or not all(_is_int(o, 1) for o in ops):
                    self.err(
                        path + ("telegram", "operators"),
                        "operators must be a list of Telegram user ids (numbers)",
                    )
                elif not ops:
                    self.warn(
                        path + ("telegram", "operators"),
                        "operators is empty: nobody can use the bots",
                    )
                if "bot_to_bot" in telegram:
                    self.need(
                        path + ("telegram", "bot_to_bot"),
                        telegram["bot_to_bot"],
                        bool,
                        "bot_to_bot",
                    )
        order = fleet.get("alert_order")
        if not isinstance(order, list) or not all(isinstance(n, str) for n in order):
            self.err(path + ("alert_order",), "alert_order must be a list of node names")
        else:
            seen: set[str] = set()
            for i, name in enumerate(order):
                if name in seen:
                    self.err(path + ("alert_order", i), f"'{name}' is listed twice")
                elif name not in nodes:
                    self.err(path + ("alert_order", i), f"'{name}' is not a node")
                seen.add(name)
            for name in nodes:
                if name not in seen:
                    self.err(path + ("alert_order",), f"alert_order does not list node '{name}'")
        update = fleet.get("update")
        if update is not None:
            update = self.mapping(path + ("update",), update, {"repo"}, "fleet.update")
            if update is not None and not (
                isinstance(update.get("repo"), str)
                and re.match(r"^(https://|git@|ssh://)\S+$", update["repo"])
            ):
                self.err(
                    path + ("update", "repo"), "update.repo must be a git address (https://...)"
                )
        for key, low, high in (("suspect_after", 30, 86400), ("control_port", 1024, 65535)):
            if key in fleet and not _is_int(fleet[key], low, high):
                self.err(path + (key,), f"{key} must be a number from {low} to {high}")
        agent = fleet.get("agent")
        if agent is not None:
            agent = self.mapping(path + ("agent",), agent, AGENT_KEYS, "fleet.agent")
            for key, value in (agent or {}).items():
                if not _is_int(value, 1):
                    self.err(path + ("agent", key), f"{key} must be a positive whole number")

    # ---- sites
    def check_sites(self, sites: Any) -> dict:
        if not isinstance(sites, dict) or not sites:
            self.err(("sites",) if sites is not None else (), "sites must list at least one site")
            return {}
        for name, site in sites.items():
            path = ("sites", name)
            if not isinstance(name, str) or not NAME_RE.match(name):
                self.err(path, f"site name '{name}': use lowercase letters, digits and hyphens")
            site = self.mapping(path, site if site is not None else {}, SITE_KEYS, f"site {name}")
            if site is None:
                continue
            tz = site.get("timezone")
            if tz is not None and not (isinstance(tz, str) and _tz_ok(tz)):
                self.err(path + ("timezone",), f"unknown time zone '{tz}' (e.g. Europe/Berlin)")
            net = site.get("lan_subnet")
            if net is not None:
                try:
                    ipaddress.IPv4Network(net, strict=True)
                except (ValueError, TypeError):
                    self.err(
                        path + ("lan_subnet",),
                        f"lan_subnet '{net}' is not a network like 192.168.1.0/24",
                    )
            power = site.get("power")
            if power is not None:
                power = self.mapping(path + ("power",), power, set(POWER_STAGES), "power")
                if power is not None:
                    self.check_power(path + ("power",), power)
            relays = site.get("relays")
            if relays is not None:
                self.check_relays(path + ("relays",), relays)
        return sites

    def check_power(self, path: tuple, power: dict) -> None:
        values: list[int] = []
        for stage in POWER_STAGES:
            value = power.get(stage)
            if not _is_int(value, 0, 100):
                self.err(path, f"power.{stage} must be a battery percentage from 0 to 100")
                return
            values.append(int(value or 0))
        if not all(a > b for a, b in zip(values, values[1:], strict=False)):
            self.err(path, "power levels must fall: conserve > shed > survive > dark")

    def check_relays(self, path: tuple, relays: Any) -> None:
        if not isinstance(relays, list):
            self.err(path, "relays must be a list")
            return
        names = set()
        for i, relay in enumerate(relays):
            relay = self.mapping(path + (i,), relay, RELAY_KEYS, "a relay")
            if relay is None:
                continue
            for key in ("name", "url", "powers"):
                if not isinstance(relay.get(key), str) or not relay[key]:
                    self.err(path + (i,), f"relay needs '{key}' (text)")
            if isinstance(relay.get("url"), str) and not re.match(r"^https?://\S+$", relay["url"]):
                self.err(path + (i, "url"), "relay url must start with http:// or https://")
            if relay.get("name") in names:
                self.err(path + (i, "name"), f"relay '{relay.get('name')}' is listed twice")
            names.add(relay.get("name"))

    # ---- nodes
    def check_nodes(self, nodes: Any, sites: dict) -> dict:
        if not isinstance(nodes, dict) or not nodes:
            self.err(("nodes",) if nodes is not None else (), "nodes must list at least one node")
            return {}
        bot_ids: dict[int, str] = {}
        for name, node in nodes.items():
            path = ("nodes", name)
            if not isinstance(name, str) or not NAME_RE.match(name):
                self.err(path, f"node name '{name}': use lowercase letters, digits and hyphens")
            node = self.mapping(path, node, NODE_KEYS, f"node {name}")
            if node is None:
                continue
            site = node.get("site")
            if site not in sites:
                self.err(path + ("site",), f"node {name}: site '{site}' is not defined under sites")
            pack = node.get("packaging")
            if pack not in PACKAGINGS:
                self.err(
                    path + ("packaging",), f"packaging must be one of: {', '.join(PACKAGINGS)}"
                )
            roles = node.get("roles")
            if (
                not isinstance(roles, list)
                or not roles
                or not all(isinstance(r, str) for r in roles)
            ):
                self.err(path + ("roles",), f"node {name}: roles must be a list")
            else:
                for i, role in enumerate(roles):
                    if role != "base" and role not in ROLES:
                        known = ", ".join(("base",) + ROLES)
                        self.err(path + ("roles", i), f"unknown role '{role}' (known: {known})")
                if len(set(roles)) != len(roles):
                    self.err(path + ("roles",), f"node {name}: a role is listed twice")
                if pack == "ha-addon" and set(roles) - {"base", "ha"}:
                    self.err(
                        path + ("roles",),
                        f"node {name}: an ha-addon node can only have the role ha",
                    )
            bot_id = node.get("bot_id")
            if bot_id is not None:
                if not _is_int(bot_id, 1):
                    self.err(
                        path + ("bot_id",),
                        "bot_id must be the number before ':' in the bot's token",
                    )
                elif bot_id in bot_ids:
                    self.err(path + ("bot_id",), f"bot_id is also used by node '{bot_ids[bot_id]}'")
                else:
                    bot_ids[bot_id] = name
            if "tailscale" in node:
                self.need(path + ("tailscale",), node["tailscale"], str, "tailscale")
            if "public_key" in node and self.need(
                path + ("public_key",), node["public_key"], str, "public_key"
            ):
                if not public_key_ok(node["public_key"]):
                    self.err(
                        path + ("public_key",),
                        "public_key must be the node's Ed25519 key: base64 of 32 bytes "
                        "(the deploy prints it, and keeps it in node.pub)",
                    )
            lan = node.get("lan")
            if lan is not None:
                try:
                    ipaddress.IPv4Address(lan)
                except (ValueError, TypeError):
                    self.err(path + ("lan",), f"lan '{lan}' is not an IPv4 address")
            options = node.get("options")
            if options is not None:
                options = self.mapping(path + ("options",), options, OPTION_KEYS, "options")
                for key, value in (options or {}).items():
                    self.need(path + ("options", key), value, bool, f"options.{key}")
            self.check_node_blocks(path, node, roles if isinstance(roles, list) else [])
        return nodes

    def check_node_blocks(self, path: tuple, node: dict, roles: list) -> None:
        nvr = node.get("nvr")
        if nvr is not None:
            nvr = self.mapping(path + ("nvr",), nvr, NVR_KEYS, "nvr")
        if nvr:
            storage = nvr.get("storage")
            if storage is not None:
                storage = self.mapping(path + ("nvr", "storage"), storage, STORAGE_KEYS, "storage")
                if storage:
                    mount = storage.get("mount")
                    if mount is not None and not (isinstance(mount, str) and mount.startswith("/")):
                        self.err(
                            path + ("nvr", "storage", "mount"), "mount must be an absolute path"
                        )
                    if "uuid" in storage:
                        self.need(path + ("nvr", "storage", "uuid"), storage["uuid"], str, "uuid")
            if "archive_target" in nvr:
                self.need(
                    path + ("nvr", "archive_target"), nvr["archive_target"], str, "archive_target"
                )
            dump = nvr.get("dump_time")
            if dump is not None and not (
                isinstance(dump, str) and re.match(r"^([01]\d|2[0-3]):[0-5]\d$", dump)
            ):
                self.err(path + ("nvr", "dump_time"), 'dump_time must look like "02:00"')
        power = node.get("power")
        if power is not None:
            power = self.mapping(path + ("power",), power, NODE_POWER_KEYS, "power")
            for key, entry in (power or {}).items():
                entry = self.mapping(path + ("power", key), entry, {"address"}, key)
                if entry is not None and not (
                    isinstance(entry.get("address"), str)
                    and re.match(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", entry["address"])
                ):
                    self.err(
                        path + ("power", key, "address"),
                        "address must be a Bluetooth address AA:BB:CC:DD:EE:FF",
                    )
        archive = node.get("archive")
        if archive is not None:
            archive = self.mapping(path + ("archive",), archive, ARCHIVE_KEYS, "archive")
        if "archive" in roles:
            arrays = (archive or {}).get("arrays")
            if not isinstance(arrays, list) or not arrays:
                self.err(
                    path, "the archive role needs archive.arrays (uuid and mount of each array)"
                )
        if archive:
            for i, array in enumerate(archive.get("arrays") or []):
                array = self.mapping(
                    path + ("archive", "arrays", i), array, STORAGE_KEYS, "an array"
                )
                if array and not (
                    isinstance(array.get("mount"), str) and array["mount"].startswith("/")
                ):
                    self.err(
                        path + ("archive", "arrays", i), "each array needs an absolute mount path"
                    )
            if archive.get("samba") is not None:
                self.mapping(path + ("archive", "samba"), archive["samba"], SAMBA_KEYS, "samba")
            retention = archive.get("retention_days")
            if retention is not None:
                retention = self.mapping(
                    path + ("archive", "retention_days"),
                    retention,
                    RETENTION_KEYS,
                    "retention_days",
                )
                for key, value in (retention or {}).items():
                    if not _is_int(value, 1):
                        self.err(
                            path + ("archive", "retention_days", key),
                            f"{key} must be a positive number of days",
                        )
        ha = node.get("ha")
        if ha is not None:
            ha = self.mapping(path + ("ha",), ha, {"port"}, "ha")
            if ha is not None and "port" in ha and not _is_int(ha["port"], 1, 65535):
                self.err(path + ("ha", "port"), "port must be 1 to 65535")

    # ---- references between entries
    def check_cross(self, nodes: dict, sites: dict) -> None:
        for name, node in nodes.items():
            if not isinstance(node, dict):
                continue
            target = (
                (node.get("nvr") or {}).get("archive_target")
                if isinstance(node.get("nvr"), dict)
                else None
            )
            if isinstance(target, str):
                other = nodes.get(target)
                other_roles = other.get("roles") if isinstance(other, dict) else None
                if not isinstance(other_roles, list) or "archive" not in other_roles:
                    self.err(
                        ("nodes", name, "nvr", "archive_target"),
                        f"archive_target '{target}' is not a node with the archive role",
                    )
        for site_name, site in sites.items():
            if not isinstance(site, dict) or not isinstance(site.get("relays"), list):
                continue
            for i, relay in enumerate(site["relays"]):
                owner = relay.get("owner") if isinstance(relay, dict) else None
                if owner is not None and (
                    owner not in nodes or nodes[owner].get("site") != site_name
                ):
                    self.err(
                        ("sites", site_name, "relays", i, "owner"),
                        f"relay owner '{owner}' is not a node at site {site_name}",
                    )
        if isinstance(self.data, dict) and "registry_format" in self.data:
            self.registry(self.data, nodes)
        elif len(nodes) > 1:
            for name, node in nodes.items():
                if isinstance(node, dict) and "public_key" not in node:
                    self.warn(
                        ("nodes", name),
                        f"node {name} has no public_key: the other nodes refuse its messages "
                        "until you add the key its deploy printed",
                    )
        ha_by_site: dict[str, list[str]] = {}
        for name, node in nodes.items():
            if (
                isinstance(node, dict)
                and isinstance(node.get("roles"), list)
                and "ha" in node["roles"]
            ):
                ha_by_site.setdefault(str(node.get("site")), []).append(name)
        for site_name, names in ha_by_site.items():
            if len(names) > 1:
                self.warn(
                    ("nodes", names[1]),
                    f"site {site_name} has several ha nodes ({', '.join(names)}); the first is checked by /status",
                )

    def registry(self, top: dict, nodes: dict) -> None:
        """Structure of the registry sections; signatures are the bot's job."""
        if top["registry_format"] != REGISTRY_FORMAT:
            self.err(
                ("registry_format",),
                f"registry_format {top['registry_format']!r} is not understood (this release: {REGISTRY_FORMAT})",
            )
        owners = top.get("owners")
        if not isinstance(owners, dict) or not {"root", "ops", "signature"} <= set(owners):
            self.err(
                ("owners",), "owners must have root, ops and signature (autonomy registry init)"
            )
        if not isinstance(top.get("components", {}), dict):
            self.err(("components",), "components must be a mapping")
        if not isinstance(top.get("revoked", []), list):
            self.err(("revoked",), "revoked must be a list")
        for name, node in nodes.items():
            if isinstance(node, dict) and "public_key" not in node:
                self.err(
                    ("nodes", name),
                    f"node {name} has no public_key: every node of a registry needs one",
                )


def public_key_ok(text: Any) -> bool:
    """An Ed25519 public key as base64 of 32 bytes, or ed25519:<base64url>."""
    if not isinstance(text, str):
        return False
    try:
        if text.startswith("ed25519:"):
            raw = base64.urlsafe_b64decode(text[8:] + "=" * (-len(text[8:]) % 4))
        else:
            raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) == 32


def is_registry(fleet: dict) -> bool:
    return "registry_format" in fleet


def signature_path(path: str | Path) -> Path:
    """registry.json -> registry.sig.json (as `autonomy registry sign` writes it)."""
    p = Path(path)
    return p.with_name(p.with_suffix("").name + ".sig.json")


def registry_parts(path: str | Path, raw: bytes) -> tuple[dict, dict]:
    """The registry exactly as signed (JSON) and its signature. Raises FleetError."""
    try:
        doc = json.loads(raw)
    except ValueError:
        raise FleetError(
            [
                (
                    0,
                    "a registry must be the JSON file written by `autonomy registry sign` (registry.json)",
                )
            ]
        ) from None
    sig_file = signature_path(path)
    try:
        sig = json.loads(sig_file.read_text())
    except OSError:
        raise FleetError(
            [(0, f"{sig_file} is missing: sign the registry with `autonomy registry sign`")]
        ) from None
    except ValueError:
        raise FleetError([(0, f"{sig_file} is not JSON")]) from None
    if not isinstance(doc, dict) or not isinstance(sig, dict):
        raise FleetError([(0, "registry and signature must be JSON objects")])
    return doc, sig


def registry_summary(doc: dict, sig: dict) -> str:
    """One line for --check; verifies the signature when autonomy-core is installed here."""
    line = (
        f"registry serial {sig.get('serial')} signed by {sig.get('signer')} at {sig.get('signed_at')}, "
        f"{len(doc.get('components', {}))} component(s), {len(doc.get('revoked', []))} revoked"
    )
    try:
        import autonomy_core as core
    except ImportError:
        return line + " (signature checked by each node's bot)"
    try:
        reg = core.verify_registry(doc, sig, root=None)
    except core.Reject as why:
        raise FleetError([(0, f"the registry signature is not valid: {why}")]) from None
    return line + f"; signature OK, root fingerprint {core.fingerprint(reg.root)}"


def parse(text: str) -> tuple[dict, list[tuple[int, str]]]:
    """Return ``(fleet, warnings)`` or raise FleetError with every error found."""
    data, lines = load_text(text)
    validator = Validator(data, lines)
    validator.run()
    if validator.errors:
        raise FleetError(sorted(validator.errors))
    return data, sorted(validator.warnings)


def parse_file(path: str) -> tuple[dict, list[tuple[int, str]]]:
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise FleetError([(0, f"cannot read {path}: {exc.strerror}")]) from None
    return parse(text)


# ---------------------------------------------------------------- settings for one node


def parse_roles(value: str) -> list[str]:
    roles = [r.strip() for r in value.split(",") if r.strip()]
    bad = [r for r in roles if r != "base" and r not in ROLES]
    if bad or not roles:
        raise FleetError(
            [
                (
                    0,
                    f"--role: unknown role {', '.join(bad) or '(empty)'}; known: base, {', '.join(ROLES)}",
                )
            ]
        )
    return roles


def node_roles(fleet: dict, node: str, override: list[str] | None = None) -> list[str]:
    roles = override if override is not None else fleet["nodes"][node]["roles"]
    return ["base"] + [r for r in ROLES if r in roles]


def ha_target(fleet: dict, node: str) -> tuple[str, int] | None:
    """The Home Assistant that /status checks: the ha node at this node's site."""
    site = fleet["nodes"][node]["site"]
    for other in fleet["nodes"].values():
        if other["site"] == site and "ha" in other["roles"]:
            host = other.get("lan")
            if host:
                return host, (other.get("ha") or {}).get("port", 8123)
            return None
    return None


def settings(fleet: dict, node: str, role_override: list[str] | None = None) -> dict[str, str]:
    """The deploy script's settings for ``node``, from its entry and its site."""
    entry = fleet["nodes"][node]
    site = fleet["sites"][entry["site"]] or {}
    roles = node_roles(fleet, node, role_override)
    out: dict[str, str] = {}

    def put(name: str, value: Any) -> None:
        out[name] = ("true" if value else "false") if isinstance(value, bool) else str(value)

    host = entry["packaging"] == "host"
    put("INSTALL_HOMEASSISTANT", "ha" in roles and host)
    options = entry.get("options") or {}
    put("INSTALL_MQTT", bool(options.get("mqtt", False)))
    put("ENABLE_HWACCEL", bool(options.get("hwaccel", False)))
    nvr = entry.get("nvr") or {}
    storage = nvr.get("storage") or {}
    if storage.get("uuid"):
        put("EXTERNAL_DRIVE_UUID", storage["uuid"])
    if storage.get("mount"):
        put("EDGE_STORAGE_DIR", storage["mount"])
    power = entry.get("power") or {}
    for key, prefix in (("victron", "VICTRON"), ("renogy", "RENOGY")):
        wanted = "power" in roles and key in power
        put(f"{prefix}_INSTALL", wanted)
        if wanted:
            put(f"{prefix}_ADDRESS", power[key]["address"])
    if site.get("timezone"):
        put("TIMEZONE", site["timezone"])
    if site.get("lan_subnet"):
        put("LOCAL_SUBNET", site["lan_subnet"])
    repo = (fleet["fleet"].get("update") or {}).get("repo")
    if repo:
        put("REDEPLOY_REPO", repo)
    target = ha_target(fleet, node)
    put("HA_HOST", target[0] if target else "")
    put("HA_PORT", target[1] if target else 8123)
    put("EDGE_NODE", node)
    if entry.get("bot_id"):
        put("EDGE_BOT_ID", entry["bot_id"])
    put("EDGE_SITE", entry["site"])
    put("EDGE_OPERATORS", ",".join(str(o) for o in fleet["fleet"]["telegram"]["operators"]))
    put("FLEET_ROLES", ",".join(roles))
    # the control plane (signed heartbeats) needs somebody to talk to
    put("CONTROL_PLANE", len(fleet["nodes"]) > 1)
    put("CONTROL_PORT", fleet["fleet"].get("control_port", DEFAULT_CONTROL_PORT))
    return out


def shell_lines(values: dict[str, str]) -> str:
    return "".join(f"[[ -v {k} ]] || {k}={shlex.quote(v)}\n" for k, v in values.items())


def plan(fleet: dict, node: str, role_override: list[str] | None, warnings: list) -> str:
    entry = fleet["nodes"][node]
    values = settings(fleet, node, role_override)
    roles = values["FLEET_ROLES"].split(",")
    lines = [
        f"node {node} at site {entry['site']}, {entry['packaging']}, roles: {', '.join(roles)}",
    ]
    if role_override is not None:
        lines.append("  (roles overridden on the command line for this run)")
    shown = {
        k: v
        for k, v in values.items()
        if k not in ("FLEET_ROLES", "EDGE_NODE", "EDGE_SITE", "EDGE_OPERATORS") and v != ""
    }
    for name, value in shown.items():
        lines.append(f"  {name}={value}")
    if not values.get("HA_HOST"):
        lines.append(
            "  no Home Assistant check in /status (no ha node with a lan address at this site)"
        )
    later = [r for r in roles if r in ROLES and r not in IMPLEMENTED_ROLES]
    if later:
        lines.append(f"  roles not implemented in this release (ignored): {', '.join(later)}")
    if entry["packaging"] != "host":
        lines.append("  this node is not a host: the deploy script cannot deploy it")
    elif "nvr" not in roles:
        lines.append(
            "  without the nvr role this release cannot deploy a host (Frigate is always installed)"
        )
    for line, message in warnings:
        lines.append(f"  warning (line {line}): {message}")
    return "\n".join(lines)


def deploy_blockers(fleet: dict, node: str, role_override: list[str] | None) -> list[str]:
    """Reasons this release cannot deploy ``node`` here (the file itself is fine)."""
    entry = fleet["nodes"][node]
    roles = node_roles(fleet, node, role_override)
    placeholders = [k for k, v in settings(fleet, node, role_override).items() if "<" in v]
    if placeholders:
        return [
            f"{', '.join(placeholders)}: replace the <placeholder> from the example with a real value"
        ]
    if entry["packaging"] != "host":
        return [
            f"node {node} is packaged as {entry['packaging']}: deploy it through its add-on, not this script"
        ]
    if "nvr" not in roles:
        return [f"node {node} has no nvr role: this release always installs Frigate on a host"]
    return []


# ---------------------------------------------------------------- what the bot gets to read


def file_hash(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def _tailscale_ip(name: str | None = None) -> str:
    """The Tailscale IPv4 address of ``name`` (this machine when None), or ""."""
    cmd = ["tailscale", "ip", "-4"] + ([name] if name else [])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    first = out.stdout.split()[:1]
    if out.returncode != 0 or not first:
        return ""
    try:
        return str(ipaddress.IPv4Address(first[0]))
    except ValueError:
        return ""


def bot_config(
    fleet: dict,
    node: str,
    raw: bytes,
    version: str = "",
    resolve: Any = _tailscale_ip,
    registry: tuple[dict, dict] | None = None,
) -> tuple[dict, list[str]]:
    """What the bot reads about the fleet: this node, its peers and their addresses.

    No secret in it. Returns ``(config, addresses)``; the addresses are where this node's
    control port is published (its own LAN and Tailscale addresses).
    """
    entry = fleet["nodes"][node]
    port = fleet["fleet"].get("control_port", DEFAULT_CONTROL_PORT)
    peers = []
    for name, other in fleet["nodes"].items():
        if name == node:
            continue
        addrs: dict[str, str] = {}
        if other.get("tailscale"):
            addrs["tailscale"] = resolve(other["tailscale"]) or other["tailscale"]
        if other.get("lan") and other["site"] == entry["site"]:
            addrs["lan"] = other["lan"]
        peers.append(
            {
                "name": name,
                "site": other["site"],
                "roles": node_roles(fleet, name),
                "public_key": other.get("public_key", ""),
                "addrs": addrs,
            }
        )
    config = {
        "node": node,
        "site": entry["site"],
        "roles": node_roles(fleet, node),
        "port": port,
        "version": version,
        "fleet_hash": file_hash(raw),
        "peers": peers,
    }
    if registry is not None:
        # the bot verifies these against the root key it pinned (autonomy-core)
        config["registry"], config["registry_signature"] = registry
    own = [a for a in (entry.get("lan"), resolve(None)) if a]
    return config, own


# ---------------------------------------------------------------- one-node file from an install


def _q(value: str) -> str:
    return json.dumps(value)


def write_one_node(
    node: str, env: dict[str, str], telegram_file: str | None, ha_host: str = ""
) -> str:
    """A fleet file for this install, taken from deploy.env-style settings. No secrets."""
    if not NAME_RE.match(node):
        raise FleetError([(0, f"node name '{node}': use lowercase letters, digits and hyphens")])

    def on(name: str) -> bool:
        return env.get(name, "") == "true"

    token_id = ""
    ids = ""
    if telegram_file and Path(telegram_file).is_file():
        for raw in Path(telegram_file).read_text().splitlines():
            if raw.startswith("TELEGRAM_BOT_TOKEN="):
                token_id = raw.split("=", 1)[1].strip().split(":", 1)[0]
            elif raw.startswith("TELEGRAM_ALLOWED_IDS="):
                ids = raw.split("=", 1)[1].strip()
    operators = [int(i) for i in re.split(r"[,\s]+", ids) if i.isdigit()]
    roles = ["nvr"]
    if on("INSTALL_HOMEASSISTANT"):
        roles.append("ha")
    if on("VICTRON_INSTALL") or on("RENOGY_INSTALL"):
        roles.append("power")
    site = "site-1"
    ha_node = "home-assistant"
    # a node that runs Home Assistant itself has no separate machine to describe
    add_ha_node = bool(ha_host) and "ha" not in roles
    out = [
        "# fleet.yaml: written by deploy-edge-server.sh --write-config from this install.",
        "# No secrets here. Rename the fleet, site and node, then add your other nodes.",
        "# Check it with: ./deploy-edge-server.sh --config fleet.yaml --node " + node + " --check",
        f"schema_version: {SCHEMA_VERSION}",
        "fleet:",
        "  name: my-fleet",
        "  telegram:",
        "    operators: [" + ", ".join(str(o) for o in operators) + "]",
        "    bot_to_bot: false",
        "  alert_order: [" + ", ".join([node] + ([ha_node] if add_ha_node else [])) + "]",
    ]
    repo = env.get("REDEPLOY_REPO", "")
    if repo and repo != UPSTREAM_REPO:
        out += ["  update:", f"    repo: {_q(repo)}"]
    out += ["sites:", f"  {site}:", f"    timezone: {_q(env.get('TIMEZONE') or 'UTC')}"]
    if env.get("LOCAL_SUBNET"):
        out.append(f"    lan_subnet: {_q(env['LOCAL_SUBNET'])}")
    out += ["nodes:", f"  {node}:", f"    site: {site}", "    packaging: host"]
    out.append("    roles: [" + ", ".join(roles) + "]")
    options = [
        f"{k}: true"
        for k, var in (("mqtt", "INSTALL_MQTT"), ("hwaccel", "ENABLE_HWACCEL"))
        if on(var)
    ]
    if options:
        out.append("    options: {" + ", ".join(options) + "}")
    if token_id.isdigit():
        out.append(f"    bot_id: {token_id}")
    out.append(f"    tailscale: {node}")
    storage = []
    if env.get("EXTERNAL_DRIVE_UUID"):
        storage.append(f"uuid: {_q(env['EXTERNAL_DRIVE_UUID'])}")
    storage.append(f"mount: {_q(env.get('EDGE_STORAGE_DIR') or '/mnt/storage')}")
    out += ["    nvr:", "      storage: {" + ", ".join(storage) + "}"]
    devices = []
    for key, prefix in (("victron", "VICTRON"), ("renogy", "RENOGY")):
        if on(f"{prefix}_INSTALL") and env.get(f"{prefix}_ADDRESS"):
            devices.append(f"      {key}: {{address: {_q(env[f'{prefix}_ADDRESS'])}}}")
    if devices:
        out += ["    power:"] + devices
    if add_ha_node:
        try:
            ipaddress.IPv4Address(ha_host)
        except ValueError:
            raise FleetError(
                [
                    (
                        0,
                        f"HA_HOST '{ha_host}' is a name, not an IPv4 address: add the Home Assistant node by hand with its lan address",
                    )
                ]
            ) from None
        out += [
            f"  {ha_node}:                    # the Home Assistant that /status checks today",
            f"    site: {site}",
            "    packaging: ha-addon          # Home Assistant OS on its own machine",
            "    roles: [ha]",
            f"    lan: {ha_host}",
        ]
        if env.get("HA_PORT") and env["HA_PORT"] != "8123":
            out += ["    ha: {port: " + env["HA_PORT"] + "}"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- command line


def report(errors: list[tuple[int, str]], name: str) -> None:
    for line, message in errors:
        where = f"{name}:{line}" if line else name
        print(f"{where}: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "env"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--node", required=(name == "env"))
        p.add_argument("--role", default=None, help="comma-separated roles for this run")
    b = sub.add_parser("bot-config", help="write the fleet description the bot reads (JSON)")
    b.add_argument("file")
    b.add_argument("--node", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--version", default="")
    w = sub.add_parser("write")
    w.add_argument("--node", required=True)
    w.add_argument("--out", required=True)
    w.add_argument(
        "--telegram", default=None, help=".telegram file (read for ids; no secret is copied)"
    )
    w.add_argument("--ha-host", default="")
    args = parser.parse_args(argv)

    if args.cmd == "bot-config":
        try:
            raw = Path(args.file).read_bytes()
            fleet_data, _ = parse(raw.decode())
            if args.node not in fleet_data["nodes"]:
                raise FleetError([(0, f"node '{args.node}' is not in the file")])
        except (OSError, UnicodeDecodeError) as exc:
            report([(0, f"cannot read {args.file}: {exc}")], args.file)
            return 1
        except FleetError as exc:
            report(exc.errors, args.file)
            return 1
        try:
            parts = registry_parts(args.file, raw) if is_registry(fleet_data) else None
        except FleetError as exc:
            report(exc.errors, args.file)
            return 1
        config, own = bot_config(fleet_data, args.node, raw, args.version, registry=parts)
        tmp = Path(args.out + ".tmp")
        tmp.write_text(json.dumps(config, indent=2) + "\n")
        tmp.replace(args.out)
        print("CONTROL_PUBLISH=" + shlex.quote(" ".join(own)))
        return 0

    if args.cmd == "write":
        try:
            if Path(args.out).exists():
                raise FleetError(
                    [(0, f"{args.out} already exists; remove it or choose another name")]
                )
            text = write_one_node(args.node, dict(os.environ), args.telegram, args.ha_host)
            parse(text)  # never hand out a file that does not pass the check
        except FleetError as exc:
            report(exc.errors, args.out)
            return 1
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
        return 0

    try:
        fleet, warnings = parse_file(args.file)
        summary = ""
        if is_registry(fleet):
            raw_file = Path(args.file).read_bytes()
            summary = registry_summary(*registry_parts(args.file, raw_file))
        override = parse_roles(args.role) if args.role else None
        if args.node and args.node not in fleet["nodes"]:
            raise FleetError(
                [(0, f"node '{args.node}' is not in the file (nodes: {', '.join(fleet['nodes'])})")]
            )
    except FleetError as exc:
        report(exc.errors, args.file)
        return 1
    if args.cmd == "check":
        if summary:
            print(summary)
        if args.node:
            print(plan(fleet, args.node, override, warnings))
        else:
            for line, message in warnings:
                print(f"warning ({args.file}:{line}): {message}")
            print(f"{args.file}: ok, {len(fleet['nodes'])} node(s)")
        return 0
    blockers = deploy_blockers(fleet, args.node, override)
    if blockers:
        report([(0, b) for b in blockers], args.file)
        return 3
    sys.stdout.write(shell_lines(settings(fleet, args.node, override)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
