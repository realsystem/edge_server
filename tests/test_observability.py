# ruff: noqa: F811, F401, E402 - pytest fixtures are imported from the other test modules and used as arguments
"""Autonomy M9 on edge_server: one trace per governed action (IT-71), redacted logs and no
secrets in the repository or in what the bot writes (IT-81)."""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
core = pytest.importorskip("autonomy_core")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_governance import audit_kinds, governed, req  # noqa: E402,F401
from test_telegram_bot import (
    ALICE,
    FRIGATE_STATS,
    bot,
    code_now,
    frigate,
    host,
    reboot,
    send,
    telegram,
)  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))
import bot as botmod  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def test_it71_restart_is_one_story_and_one_trace(governed, telegram, frigate):
    send(governed, f"/restart frigate {code_now()}")
    req(governed, "restart-result").write_text(json.dumps({"service": "frigate", "status": "ok"}))
    stats = json.loads(json.dumps(FRIGATE_STATS))
    stats["service"]["uptime"] = 3  # Frigate came back: the restart is verified (M8)
    frigate.routes["/api/stats"] = (200, stats)
    governed._deliver_restart(ALICE)
    g = governed.governance
    records = list(g.audit.records())
    corr = records[0]["correlationid"]
    story = core.reconstruct(corr, records, core.telemetry.read_spans(g.dir / "traces.jsonl"))
    assert story["kinds"] == [
        "action.evaluated",
        "action.decided",
        "action.started",
        "action.succeeded",
        "action.verified",
    ]
    assert len(story["trace_ids"]) == 1  # one trace from the command to the verification
    assert story["spans"][0] == "telegram.command"
    assert {
        "evaluator.evaluate",
        "pdp.decide",
        "gateway.execute",
        "adapter.execute",
        "gateway.complete",
        "verifier.check",
    } <= set(story["spans"])
    send(governed, "/audit")
    assert f"[trace {story['trace_ids'][0][:8]}]" in telegram.texts()[-1]


def test_bot_logs_are_redacted(monkeypatch):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    token = "987654321:" + "AA" + "y" * 33
    monkeypatch.setenv("HA_TOKEN", "ha-long-lived-" + "token-123")
    try:
        cfg = botmod.Config(token=token, totp_secret="JBSWY3DPEHPK3PXP")
        assert botmod.install_redaction(cfg)
        logging.getLogger("edge-bot").warning(
            "calling %s with %s and %s", token, "ha-long-lived-" + "token-123", "JBSWY3DPEHPK3PXP"
        )
    finally:
        root.removeHandler(handler)
    out = stream.getvalue()
    assert token not in out and "ha-long-lived" not in out and "JBSWY3DPEHPK3PXP" not in out
    assert out.count("[REDACTED]") == 3


def test_it81_repository_has_no_secrets():
    findings = core.scan([REPO], allowlist=REPO / ".secrets-allowlist", root=REPO)
    assert findings == [], "\n".join(map(str, findings))


def test_it81_what_the_bot_writes_has_no_secrets(governed):
    send(governed, f"/restart frigate {code_now()}")
    send(governed, f"/reboot {code_now(1)}")
    written = [
        p
        for p in governed.governance.dir.rglob("*")
        if p.is_file() and not p.name.endswith((".key", ".pem"))
    ]
    assert written
    for p in written:
        text = p.read_text()
        assert governed.cfg.token not in text and governed.cfg.totp_secret not in text, p
        assert core.scan_text(text, str(p)) == [], p
