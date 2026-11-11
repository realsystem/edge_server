# ruff: noqa: F811, F401, E402 - pytest fixtures are imported from the other test modules and used as arguments
"""Autonomy M8 on edge_server: each governed host action is evaluated before the decision
(C13) and verified afterwards (C14) through a probe that, where possible, does not trust
the host helper: the host's boot time after /reboot and Frigate's own uptime after
/restart frigate."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
core = pytest.importorskip("autonomy_core")
if not hasattr(core, "Verifier"):
    pytest.skip("autonomy-core without the M8 verifier", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_governance import audit_kinds, governed, req
from test_telegram_bot import (
    ALICE,
    FRIGATE_STATS,
    NOW,
    bot,
    code_now,
    frigate,
    host,
    reboot,
    send,
    telegram,
)


def records(b, kind):
    return [r for r in b.governance.audit.records() if r["kind"] == kind]


def restart_frigate(b):
    send(b, f"/restart frigate {code_now()}")
    aid = b._governed["restart"]
    req(b, "restart-result").write_text(json.dumps({"service": "frigate", "status": "ok"}))
    assert b._deliver_restart(ALICE)
    return aid


def frigate_uptime(frigate, seconds):
    stats = copy.deepcopy(FRIGATE_STATS)
    stats["service"]["uptime"] = seconds
    frigate.routes["/api/stats"] = (200, stats)


def test_evaluated_before_the_decision(governed):
    send(governed, f"/restart frigate {code_now()}")
    kinds = audit_kinds(governed)
    assert kinds[:3] == ["action.evaluated", "action.decided", "action.started"]
    (ev,) = records(governed, "action.evaluated")
    assert ev["summary"].startswith("ok: host.restart_service on frigate")
    assert ev["correlationid"] == records(governed, "action.decided")[0]["correlationid"]


def test_frigate_restart_confirmed_by_frigate_itself(governed, frigate, telegram):
    aid = restart_frigate(governed)
    # Frigate still reports the old process (up 25 h): not yet confirmed, ask again later
    assert governed.governance.verifier.verification(aid) is None
    frigate_uptime(frigate, 4)
    n = len(telegram.texts())
    governed.verify_actions()
    v = governed.governance.verifier.verification(aid)
    assert v["result"] == "confirmed" and v["independent"]
    assert v["observations"][0]["path"] == "frigate.uptime"
    assert len(telegram.texts()) == n  # confirmed outcomes are not announced
    assert audit_kinds(governed)[-1] == "action.verified"
    assert "confirmed (independently)" in records(governed, "action.verified")[0]["summary"]
    assert (
        core.verify_chain(list(governed.governance.audit.records()), governed.governance.keys) == []
    )
    send(governed, "/audit")
    assert "action.verified: confirmed" in telegram.texts()[-1]


def test_helper_said_ok_but_frigate_never_restarted(governed, frigate, telegram):
    """ST-12 on this node: the helper reports success, Frigate's own uptime says otherwise."""
    aid = restart_frigate(governed)
    governed.clock = lambda: NOW + 601  # the window is over; Frigate is still the old process
    governed.governance.clock = governed.clock
    governed.governance.verifier.clock = governed.clock
    governed.verify_actions()
    v = governed.governance.verifier.verification(aid)
    assert v["result"] == "contradicted" and v["independent"]
    assert audit_kinds(governed)[-2:] == ["action.verified", "incident.opened"]
    assert "no undo declared" in records(governed, "incident.opened")[0]["summary"]
    warning = telegram.texts()[-1]
    assert warning.startswith("Warning: host.restart_service frigate was reported done")
    governed.verify_actions()  # settled once
    assert audit_kinds(governed)[-1] == "incident.opened"


def test_frigate_unreachable_is_inconclusive_at_the_end(governed, frigate, telegram):
    aid = restart_frigate(governed)
    frigate.routes["/api/stats"] = (500, {})
    governed.verify_actions()
    assert governed.governance.verifier.verification(aid) is None
    governed.verify_actions(final=True)
    v = governed.governance.verifier.verification(aid)
    assert v["result"] == "inconclusive" and "Frigate stats unavailable" in v["note"]
    assert telegram.texts()[-1].startswith("Could not check host.restart_service frigate")


def test_other_services_and_redeploy_use_the_job_result(governed):
    send(governed, f"/redeploy {code_now()}")
    req(governed, "redeploy-result").write_text(json.dumps({"status": "ok", "at": 0}))
    governed.deliver_redeploy_result()
    assert audit_kinds(governed)[-2:] == ["action.succeeded", "action.verified"]
    (v,) = records(governed, "action.verified")
    assert "confirmed (via the same channel)" in v["summary"]


def test_reboot_is_verified_by_the_host_boot_time(governed, monkeypatch):
    g = governed.governance
    rec = g.request("host.reboot", "host", requested_by=ALICE, message_id=7)
    assert rec["status"] == "started"
    waiting = g.gateway.entries[rec["action_id"]]
    assert waiting["expect"]["verification"] == {"probe": "host.boot_time", "independent": True}
    g.finished(rec["action_id"], True)
    # uptime 3600 s: the host booted an hour before the request, so not yet
    assert g.verifier.verification(rec["action_id"]) is None
    monkeypatch.setattr(governed, "uptime", lambda: 2.0)  # the new boot
    (v,) = g.verify_pending()
    assert v["result"] == "confirmed" and v["independent"]
    assert v["observations"][0]["path"] == "host.boot_time"


def test_unknown_service_is_rejected_by_the_evaluator(governed):
    g = governed.governance
    rec = g.request("host.restart_service", "nope", requested_by=ALICE, message_id=8)
    assert rec["status"] == "rejected" and rec["error"]["code"] == "policy.denied"
    (ev,) = records(governed, "action.evaluated")
    assert ev["summary"].startswith("reject:") and "invariant.known_service" in ev["summary"]
    assert not req(governed, "restart-request").exists()


def test_verification_survives_a_bot_restart(governed, frigate):
    aid = restart_frigate(governed)
    governed.governance = None
    governed.setup_governance()  # a new process, same state directory
    frigate_uptime(frigate, 4)
    governed.verify_actions()
    assert governed.governance.verifier.verification(aid)["result"] == "confirmed"
    governed.governance = None
    governed.setup_governance()
    assert governed.governance.verifier.verification(aid)["result"] == "confirmed"
    assert governed.governance.verify_pending() == []
