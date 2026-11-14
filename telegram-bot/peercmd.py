"""Commands between fleet nodes (ADR-018): what a peer may ask of this node, and how it is asked.

A request to restart a named service or reboot the host arrives as the ``request`` of a fleet
``control.command_request`` message and is a signed ``peer-request`` that carries the OWNER's TOTP
code. ``autonomy_core.peer.RequestGuard`` checks it; this module holds the parts that belong to the
bot: the grants from ``PEER_GRANTS`` and the texts.

PEER_GRANTS syntax: ``node:action[,action...][;node:...]`` where an action is ``reboot`` or
``restart=svc1|svc2``. Example: ``pi-1:restart=frigate|mosquitto,reboot``. Empty (the default)
means no peer may ask for anything.
"""

from __future__ import annotations

import re
from typing import Any

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SERVICE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


def parse_grants(text: str) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """node -> (actions, services), with the actions named as the spec names them."""
    grants: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for entry in (e.strip() for e in text.split(";")):
        if not entry:
            continue
        node, sep, rest = entry.partition(":")
        node = node.strip()
        if not sep or not NAME.match(node):
            raise ValueError(f"PEER_GRANTS: '{entry}' must look like node:reboot,restart=svc|svc")
        actions: set[str] = set()
        services: set[str] = set()
        for item in (i.strip() for i in rest.split(",")):
            if item == "reboot":
                actions.add("reboot_host")
            elif item.startswith("restart="):
                names = [n for n in item[len("restart=") :].split("|") if n]
                if not names or not all(SERVICE.match(n) for n in names):
                    raise ValueError(f"PEER_GRANTS: '{item}' needs service names")
                actions.add("restart_service")
                services.update(names)
            else:
                raise ValueError(f"PEER_GRANTS: unknown action '{item}' (reboot, restart=svc|svc)")
        if not actions:
            raise ValueError(f"PEER_GRANTS: '{entry}' grants nothing")
        grants[node] = (frozenset(actions), frozenset(services))
    return grants


def describe(request: dict[str, Any]) -> str:
    if request.get("action") == "reboot_host":
        return "reboot the host"
    return f"restart {request.get('service')}"
