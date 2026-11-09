# ruff: noqa: F811 - pytest fixtures imported from test_telegram_bot are used as arguments
"""Host actions through the PDP and the Action Gateway (Autonomy M5).

The owner's /restart, /reboot and /redeploy become proposals; the PDP decides; only the
gateway writes the request for the host helpers; every step lands in a signed,
hash-chained audit log (/audit).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
core = pytest.importorskip("autonomy_core")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_telegram_bot import (  # noqa: E402,F401
    ALICE,
    COMPOSE,
    NOW,
    bot,
    code_now,
    frigate,
    host,
    reboot,
    send,
    telegram,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))
import governance as gov  # noqa: E402


def req(b, name):
    return b.cfg.requests_dir / name


@pytest.fixture
def governed(reboot, monkeypatch, tmp_path):
    """/reboot's setup plus a state directory: the bot routes host actions through the gateway."""
    state = tmp_path / "state"
    state.mkdir()
    reboot.cfg = reboot.cfg.__class__(
        **{**reboot.cfg.__dict__, "control_state": state, "control_key": state / "node.key"}
    )
    (reboot.cfg.app_dir / "docker-compose.yml").write_text(COMPOSE)
    reboot.watches = []
    monkeypatch.setattr(
        reboot,
        "_start_job_watch",
        lambda kind, chat, wait, deliver: reboot.watches.append((kind, chat)),
    )
    monkeypatch.setattr(
        reboot, "_start_redeploy_watch", lambda chat: reboot.watches.append(("redeploy", chat))
    )
    monkeypatch.setattr(reboot, "uptime", lambda: 3600.0)
    reboot.setup_governance()
    assert reboot.governance is not None
    return reboot


def audit_kinds(b):
    return [r["kind"] for r in b.governance.audit.records()]


class TestGovernedCommands:
    def test_restart_goes_through_pdp_and_gateway(self, governed, telegram):
        send(governed, f"/restart frigate {code_now()}")
        assert json.loads(req(governed, "restart-request").read_text()) == {"service": "frigate"}
        assert "Restarting frigate" in telegram.texts()[-1]
        aid = governed._governed["restart"]
        assert governed.governance.gateway.record(aid)["status"] == "started"
        assert audit_kinds(governed) == ["action.decided", "action.started"]
        # the host helper answers
        req(governed, "restart-result").write_text(
            json.dumps({"service": "frigate", "status": "ok"})
        )
        assert governed._deliver_restart(ALICE)
        assert governed.governance.gateway.record(aid)["status"] == "succeeded"
        assert audit_kinds(governed)[-1] == "action.succeeded"
        assert (
            core.verify_chain(list(governed.governance.audit.records()), governed.governance.keys)
            == []
        )

    def test_failed_restart_is_recorded(self, governed):
        send(governed, f"/restart frigate {code_now()}")
        req(governed, "restart-result").write_text(
            json.dumps({"service": "frigate", "status": "failed", "detail": "exit 1"})
        )
        governed._deliver_restart(ALICE)
        assert audit_kinds(governed)[-1] == "action.failed"

    def test_reboot_and_the_notice_after_it(self, governed, telegram):
        send(governed, f"/reboot {code_now()}")
        assert req(governed, "reboot").exists() and "Rebooting now" in telegram.texts()[-1]
        aid = governed._governed["reboot"]
        decision = [
            r for r in governed.governance.audit.records() if r["kind"] == "action.decided"
        ][0]
        assert (
            "require_approval" in decision["summary"]
        )  # R2: the code becomes an attested approval
        assert governed.governance.approvals.pending() == []
        # the bot comes back after the reboot: a new process, same state
        governed.governance = None
        governed._governed.clear()
        governed.clock = lambda: NOW + 120  # two minutes later, up for 30 s
        governed.setup_governance()
        assert governed.governance.pending("reboot") == aid  # async action survives the restart
        governed.uptime = lambda: 30.0
        governed.notify_after_reboot()
        assert governed.governance.gateway.record(aid)["status"] == "succeeded"

    def test_redeploy(self, governed):
        send(governed, f"/redeploy {code_now()}")
        assert req(governed, "redeploy").exists()
        req(governed, "redeploy-result").write_text(json.dumps({"status": "ok", "at": 0}))
        governed.deliver_redeploy_result()
        assert audit_kinds(governed)[-1] == "action.succeeded"

    def test_policy_can_forbid(self, governed, telegram, monkeypatch):
        forbid = core.PolicyBundle(
            "2026.11.09-1", (core.Policy("no-reboot", "forbid (principal, action, resource);"),)
        )
        governed.governance.pdp.bundle = forbid
        governed.governance.pdp._cedar = lambda request, policies, entities: (
            False,
            ["policy0"],
            [],
        )
        send(governed, f"/reboot {code_now()}")
        assert "policy.denied" in telegram.texts()[-1]
        assert not req(governed, "reboot").exists() and not req(governed, "reboot-notify").exists()
        assert audit_kinds(governed)[-1] == "action.rejected"

    def test_audit_command(self, governed, telegram):
        send(governed, f"/restart frigate {code_now()}")
        send(governed, "/audit")
        text = telegram.texts()[-1]
        assert (
            "chain intact" in text and "action.started" in text and "policy bundle baseline" in text
        )

    def test_no_state_dir_keeps_the_direct_path(self, reboot, telegram):
        (reboot.cfg.app_dir / "docker-compose.yml").write_text(COMPOSE)
        reboot.setup_governance()
        assert reboot.governance is None
        send(reboot, "/audit")
        assert "audit log is off" in telegram.texts()[-1]


class TestGovernanceUnit:
    def make(self, tmp_path, submitted, clock=lambda: 1_800_000_000.0, **kw):
        key = gov.load_or_create_key(tmp_path / "k.pem")
        return gov.Governance(
            tmp_path,
            key=key,
            cell="field-station",
            submit={"restart": lambda p: submitted.append(p) or True, "reboot": lambda p: False},
            clock=clock,
            operators=[1],
            **kw,
        )

    def test_key_is_created_once(self, tmp_path):
        a = gov.load_or_create_key(tmp_path / "k.pem")
        b = gov.load_or_create_key(tmp_path / "k.pem")
        assert core.encode_public_key(a) == core.encode_public_key(b)
        assert (tmp_path / "k.pem").stat().st_mode & 0o777 == 0o600

    def test_records_conform_and_idempotent(self, tmp_path):
        submitted = []
        g = self.make(tmp_path, submitted)
        rec = g.request("host.restart_service", "frigate", requested_by=1, message_id=5)
        again = g.request("host.restart_service", "frigate", requested_by=1, message_id=5)
        assert rec == again and len(submitted) == 1  # the same Telegram message runs once
        assert (
            submitted[0]["service"] == "frigate" and submitted[0]["action_id"] == rec["action_id"]
        )
        g.finished(rec["action_id"], True)
        g.finished(rec["action_id"], False)  # a late duplicate result changes nothing
        assert g.gateway.record(rec["action_id"])["status"] == "succeeded"

    def test_pending_job_is_a_failure_not_a_second_request(self, tmp_path):
        g = self.make(tmp_path, [])
        rec = g.request("host.reboot", "host", requested_by=1, message_id=6)
        assert rec["status"] == "failed" and "already pending" in rec["error"]["message"]

    def test_signed_bundle_and_refused_bundle(self, tmp_path):
        (tmp_path / "bundle.json").write_text(json.dumps({"bundle": "2026.11.09-1"}))
        g = self.make(tmp_path, [], bundle_file=tmp_path / "bundle.json")
        assert g.pdp.bundle.version == "baseline" and "policy bundle refused" in g.audit_text()

    def test_suspended(self, tmp_path):
        g = self.make(tmp_path, [], suspended=lambda: True)
        assert (
            g.request("host.restart_service", "frigate", requested_by=1, message_id=7)["error"][
                "code"
            ]
            == "policy.suspended"
        )

    def test_cell_names(self):
        assert gov.cell_name({"node": "Home_Hub"}) == "home-hub"
        assert gov.cell_name({"node": "!!!"}) == "local"


def agent_wants_restart(g):
    """An agent's proposal that an approvable policy sends to the owner."""
    g.pdp.bundle = core.PolicyBundle(
        "2026.11.09-1",
        (core.Policy("ask-owner", "permit (principal, action, resource);", approvable=True),),
    )
    g.pdp._cedar = lambda request, policies, entities: (
        (True, ["policy0"], []) if "approval" in request["context"] else (False, [], [])
    )
    p = {
        "proposal_id": core.prefixed_id("prp"),
        "proposer": "agent:fleet-doctor",
        "objective": {"id": "obj_frigate_up", "text": "keep Frigate recording"},
        "action": {"type": "host.restart_service", "target": "frigate", "params": {}},
        "rationale": "frigate stopped answering",
        "inputs": [],
        "expected_effect": {"kind": "service_running", "within_s": 300},
        "verification": {"probe": "host.service_state", "independent": False},
        "reversibility": {"irreversible": True},
        "freshness_s": 300,
        "idempotency_key": "doctor:frigate:1",
    }
    d = g.pdp.decide(core.decision_request(p, cell=g.cell, risk_class="R1"))
    assert d["effect"] == "require_approval"
    return g.ask(p, d, "restart frigate (fleet doctor)")


class TestApprovalsAndControls:
    def test_pending_approve_executes(self, governed, telegram):
        q = agent_wants_restart(governed.governance)
        send(governed, "/pending")
        assert q["request_id"] in telegram.texts()[-1] and "restart frigate" in telegram.texts()[-1]
        send(governed, f"/approve {q['request_id']} {code_now()}")
        assert "Approved; the action is started" in telegram.texts()[-1]
        assert json.loads(req(governed, "restart-request").read_text()) == {"service": "frigate"}
        assert audit_kinds(governed)[:2] == ["approval.requested", "action.started"]

    def test_approve_needs_a_good_code_and_a_known_id(self, governed, telegram):
        agent_wants_restart(governed.governance)
        send(governed, "/approve apq_x 000000")
        assert not req(governed, "restart-request").exists()
        send(governed, f"/approve apq_unknown {code_now()}")
        assert "Not approved" in telegram.texts()[-1] and "not pending" in telegram.texts()[-1]
        assert governed.governance.approvals.pending()  # still waiting

    def test_approve_in_a_group_is_refused(self, governed, telegram):
        q = agent_wants_restart(governed.governance)
        send(governed, f"/approve {q['request_id']} {code_now()}", chat=-500, chat_type="group")
        assert "private chat" in telegram.texts()[-1]
        assert not req(governed, "restart-request").exists()

    def test_deny(self, governed, telegram):
        q = agent_wants_restart(governed.governance)
        send(governed, f"/deny {q['request_id']}")
        assert "will not run" in telegram.texts()[-1]
        assert governed.governance.approvals.pending() == []
        assert audit_kinds(governed)[-1] == "approval.denied"
        send(governed, f"/deny {q['request_id']}")
        assert "not waiting" in telegram.texts()[-1]

    def test_hold_stops_automatic_actions_not_the_owner(self, governed, telegram):
        send(governed, "/hold storm coming")
        assert "Hold all" in telegram.texts()[-1]
        q = agent_wants_restart(governed.governance)
        send(governed, f"/approve {q['request_id']} {code_now()}")
        assert "policy.suspended" in telegram.texts()[-1] and "on hold" in telegram.texts()[-1]
        assert not req(governed, "restart-request").exists()
        send(governed, f"/restart frigate {code_now(1)}")  # the owner's own command still works
        assert req(governed, "restart-request").exists()
        send(governed, "/audit")
        assert "ACTIVE: hold all" in telegram.texts()[-1]

    def test_suspend_survives_a_restart_and_resume_needs_a_code(self, governed, telegram):
        send(governed, "/suspend agent:fleet-doctor misbehaving")
        assert "Suspend agent:fleet-doctor" in telegram.texts()[-1]
        governed.governance = None
        governed.setup_governance()
        assert governed.governance.control.blocked("agent:fleet-doctor")
        send(governed, "/resume agent:fleet-doctor 000000")
        assert governed.governance.control.blocked("agent:fleet-doctor")
        send(governed, f"/resume agent:fleet-doctor {code_now()}")
        assert "Resumed agent:fleet-doctor" in telegram.texts()[-1]
        assert governed.governance.control.blocked("agent:fleet-doctor") is None
        assert audit_kinds(governed)[-1] == "system.resumed"

    def test_usage(self, governed, telegram):
        send(governed, "/suspend")
        assert "Send /suspend" in telegram.texts()[-1]
        send(governed, "/approve")
        assert "Send /approve" in telegram.texts()[-1]
