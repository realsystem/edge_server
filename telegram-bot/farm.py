"""/farm: what the Field Station cell last saw (Autonomy M10). Read only, standard library only.

The cell (autonomy-core, ``autonomy station run``) writes a small ``status.json`` next to its event
log. This module reads that file and formats it for Telegram. It never opens the event log, never
talks to Home Assistant and has no way to change anything: if the file is missing, old or broken
it says so instead of guessing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

STALE_AFTER_S = 120.0  # the cell writes at least every heartbeat (30 s by default)


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


def farm_text(path: Path, now: Callable[[], float] = time.time) -> str:
    doc = load(path)
    if doc is None:
        return (
            "The Field Station cell has not reported: no readable status file. "
            "Is the cell running? (autonomy station run)"
        )
    t = now()
    age = t - float(doc.get("updated_at", 0) or 0)
    entities: dict[str, dict[str, Any]] = doc["entities"]
    if age > STALE_AFTER_S:
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
