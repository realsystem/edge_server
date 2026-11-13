"""/farm: what the Field Station cell last saw (Autonomy M10). Read only, standard library only.

The cell (autonomy-core, ``autonomy station run``) writes a small ``status.json`` next to its event
log. This module reads that file and formats it for Telegram. It never opens the event log, never
talks to Home Assistant and has no way to change anything: if the file is missing, old or broken
it says so instead of guessing.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STALE_AFTER_S = 120.0  # the cell writes at least every heartbeat (30 s by default)
HYSTERESIS_PERCENT = (
    5.0  # a threshold alert ends only when the value is this far back (of the limit)
)


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _num(state: str) -> str:
    try:
        v = float(state)
    except ValueError:
        return state
    return f"{v:.0f}" if v == int(v) and abs(v) >= 100 else f"{v:g}"


def load(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("entities"), dict) else None


def farm_text(
    path: Path, now: Callable[[], float] = time.time, stale_after: float = STALE_AFTER_S
) -> str:
    doc = load(path)
    if doc is None:
        return (
            "The Field Station cell has not reported: no readable status file. "
            "Is the cell running? (autonomy station run)"
        )
    t = now()
    age = t - float(doc.get("updated_at", 0) or 0)
    entities: dict[str, dict[str, Any]] = doc["entities"]
    if age > stale_after:
        head = f"Field Station: NOT REPORTING (last word {ago(age)} ago). Last known values:"
    elif doc.get("connected"):
        head = f"Field Station: connected to Home Assistant, {len(entities)} entities, up {ago(doc.get('uptime_s', 0))}."
    else:
        head = "Field Station: running, but Home Assistant is unreachable. Last known values:"
    if not entities:
        return head + "\nNo allowlisted entity has been seen yet."
    lines = [head]
    for entity_id in sorted(entities):
        e = entities[entity_id]
        name = str(e.get("name") or entity_id)
        unit = str(e.get("unit") or "")
        value = _num(str(e.get("state", "?")))
        seen = ago(t - float(e.get("observed_at", t) or t))
        lines.append(f"{name}: {value}{(' ' + unit) if unit else ''}  ({seen} ago)")
    return "\n".join(lines)


# -- alerts ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    entity: str
    op: str  # "<" or ">"
    limit: float

    @property
    def text(self) -> str:
        return f"{self.entity}{self.op}{self.limit:g}"


_RULE = re.compile(r"^\s*([a-z0-9_]+\.[a-z0-9_]+)\s*([<>])\s*(-?[0-9]+(?:\.[0-9]+)?)\s*$")


def parse_rules(spec: str) -> tuple[list[Rule], list[str]]:
    """``sensor.wh51_ch1_soil_moisture<20, sensor.ws90_temperature>40`` -> rules and the entries
    that could not be read (reported once in the log; they never stop the bot)."""
    rules: list[Rule] = []
    bad: list[str] = []
    for part in re.split(r"[,;\n]", spec):
        if not part.strip():
            continue
        m = _RULE.match(part)
        if m is None:
            bad.append(part.strip())
        else:
            rules.append(Rule(m.group(1), m.group(2), float(m.group(3))))
    return rules, bad


def station_problem(
    doc: dict[str, Any] | None, now: float, stale_after: float = STALE_AFTER_S
) -> str | None:
    """Why the Field Station cell is not doing its job, or None."""
    if doc is None:
        return "Field Station: no readable status file. Is the cell running?"
    age = now - float(doc.get("updated_at", 0) or 0)
    if age > stale_after:
        return f"Field Station: not reporting for {ago(age)}. Is the cell running?"
    if not doc.get("connected"):
        return (
            "Field Station: running, but it cannot reach Home Assistant. Readings are not updating."
        )
    return None


def label(doc: dict[str, Any], entity: str) -> str:
    return str((doc["entities"].get(entity) or {}).get("name") or entity)


def rule_state(rule: Rule, doc: dict[str, Any]) -> tuple[str, str]:
    """("bad", why) / ("ok", "") / ("band", "") for one rule: band is the hysteresis zone, where
    an alert that is on stays on and one that is off stays off."""
    e = doc["entities"].get(rule.entity)
    if e is None:
        return (
            "bad",
            f"{rule.entity}: no reading (not on the allowlist, or Home Assistant does not have it)",
        )
    name = label(doc, rule.entity)
    unit = str(e.get("unit") or "")
    try:
        value = float(e["state"])
    except (KeyError, TypeError, ValueError):
        return "bad", f"{name}: no usable reading ({e.get('state', '?')})"
    shown = f"{_num(str(e['state']))}{(' ' + unit) if unit else ''}"
    margin = abs(rule.limit) * HYSTERESIS_PERCENT / 100.0
    if rule.op == "<":
        if value < rule.limit:
            return "bad", f"{name} is low: {shown} (alert below {rule.limit:g})."
        return ("ok", "") if value >= rule.limit + margin else ("band", "")
    if value > rule.limit:
        return "bad", f"{name} is high: {shown} (alert above {rule.limit:g})."
    return ("ok", "") if value <= rule.limit - margin else ("band", "")
