"""Governed execution of host actions: PDP -> Action Gateway -> host helpers -> audit.

Autonomy milestone M5 (realsystem/autonomy, components C04, C06, C16). The owner's
/restart, /reboot and /redeploy (TOTP-checked) become proposals by ``owner:ops``; the
policy decision point decides; the action gateway is the only code that writes the
request for the host helpers (the drop box in REQUESTS_DIR); every step is a signed,
hash-chained audit record in the state directory, readable with /audit.

Autonomy M8 (C13, C14): each proposal is evaluated first (the outcome feeds the PDP), and
each executed action is verified afterwards through a probe: the host's boot time after a
reboot and Frigate's own uptime after a Frigate restart (both independent of the host
helper), the host helper's job result otherwise. A contradicted outcome opens an incident
in the audit log and the owner is told.

Needs ``autonomy-core`` (and ``cryptography``) and a writable state directory; the bot
falls back to its direct path when either is missing and says so in its log.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import autonomy_core as core
from autonomy_core.gateway import ActionGateway, AdapterResult
from autonomy_core.pdp import Pdp, PolicyBundle, decision_request, load_bundle

log = logging.getLogger("edge-bot.governance")

# action type -> (drop-box job kind, risk class)
ACTIONS = {
    "host.restart_service": ("restart", "R1"),
    "host.reboot": ("reboot", "R2"),
    "host.redeploy": ("redeploy", "R2"),
}


class HostHelperAdapter:
    """C20 adapter for edge_server's host helpers. The bot hands it the functions that
    write a request into the drop box; the outcome arrives later (asynchronous)."""

    def __init__(self, name: str, submit: dict[str, Callable[[dict[str, Any]], bool]]):
        self.name = name
        self.submit = submit

    def actions(self) -> set[str]:
        return {a for a, (kind, _) in ACTIONS.items() if kind in self.submit}

    def execute(self, action_id: str, action: Any) -> AdapterResult:
        kind, _ = ACTIONS[action["type"]]
        payload = dict(action.get("params", {}))
        if kind == "restart":
            payload["service"] = action["target"]
        payload["action_id"] = action_id
        if not self.submit[kind](payload):
            return AdapterResult("failed", error=f"a {kind} is already pending")
        return AdapterResult("started", {"requested": kind})

    def reconcile(self, action_id: str, action: Any) -> AdapterResult | None:
        return None  # the bot settles host jobs from their result files


def cell_name(fleet_config: dict[str, Any] | None) -> str:
    name = str((fleet_config or {}).get("node") or socket.gethostname()).lower()
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "local"
    return name if core.is_name(name) else "local"


def load_or_create_key(path: Path) -> Any:
    try:
        return core.load_private_key(path)
    except (OSError, ValueError):
        key = core.generate_private_key()
        core.save_private_key(key, path)
        log.info("governance: created %s", path)
        return key


class Governance:
    def __init__(
        self,
        state_dir: Path,
        *,
        key: Any,
        cell: str,
        submit: dict[str, Callable[[dict[str, Any]], bool]],
        registry: Any = None,
        bundle_file: Path | None = None,
        clock: Callable[[], float] = time.time,
        suspended: Callable[[], bool] = lambda: False,
        operators: Any = (),
        boot_time: Callable[[], float | None] | None = None,
        frigate_started: Callable[[], float | None] | None = None,
        services: Callable[[], list[str]] | None = None,
    ):
        self.cell = cell
        self.clock = clock
        self.dir = state_dir
        self.issuer = f"svc:pdp@{cell}"
        self.executor = f"svc:action-gateway@{cell}"
        public = core.encode_public_key(key)
        self.attester = f"svc:approval@{cell}"
        self.keys = core.KeyRing(
            {
                "svc:pdp": public,
                "svc:action-gateway": public,
                "svc:approval": public,
                "svc:verifier": public,
            }
        )
        # C05: approvals and emergency controls (Autonomy M6), signed with the node key
        self.approvals = core.ApprovalService(
            state_dir, key=key, attester=self.attester, operators=operators, clock=clock
        )
        self.control = core.ControlState(
            state_dir, key=key, attester=self.attester, cell=cell, clock=clock
        )
        bundle = PolicyBundle.baseline()
        self.bundle_problem = ""
        if bundle_file is not None and bundle_file.exists():
            try:
                bundle = load_bundle(json.loads(bundle_file.read_text()), registry, now=clock())
            except (ValueError, OSError, core.Reject) as exc:
                self.bundle_problem = f"policy bundle refused: {exc}"
                log.warning("governance: %s; using the baseline", self.bundle_problem)
        # one trace per action story (Autonomy M9, IT-71): OTLP/JSON spans next to the audit log
        self.tracer = core.Tracer(
            state_dir / "traces.jsonl", service=f"cell:{cell}", instance=f"cell:{cell}"
        )
        self.pdp = Pdp(issuer=self.issuer, key=key, bundle=bundle, clock=clock, tracer=self.tracer)
        self.audit = core.AuditLog(
            state_dir / "audit.jsonl", cell=cell, actor=self.executor, key=key
        )
        self.adapter = HostHelperAdapter(f"adapter:host@{cell}", submit)
        self.gateway = ActionGateway(
            executor=self.executor,
            key=key,
            state_dir=state_dir / "gateway",
            pdp_keys=self.keys,
            registry=registry,
            adapters=[self.adapter],
            audit=self.audit,
            clock=clock,
            suspended=suspended,
            blocked=self.control.blocked,
            approval_keys=core.KeyRing({"svc:approval": public}),
            tracer=self.tracer,
        )
        for rec in self.gateway.reconcile():
            log.warning(
                "governance: %s settled as %s after a restart", rec["action_id"], rec["status"]
            )
        # C13 before, C14 after (Autonomy M8)
        self.boot_time = boot_time
        self.frigate_started = frigate_started
        self.jobs_file = state_dir / "job-results.json"
        try:
            self.jobs: dict[str, dict[str, Any]] = json.loads(self.jobs_file.read_text())
        except (OSError, ValueError):
            self.jobs = {}
        probes = {"host.job_result": core.Probe(self._probe_job, independent=False)}
        if boot_time is not None:
            probes["host.boot_time"] = core.Probe(self._probe_boot, independent=True)
        if frigate_started is not None:
            probes["frigate.uptime"] = core.Probe(self._probe_frigate, independent=True)
        invariants: dict[str, dict[str, Any]] = {}
        if services is not None:
            invariants["host.restart_service"] = {"known_service": _known_service(services)}
        self.evaluator = core.Evaluator(
            invariants=invariants,
            open_proposals=self._open_proposals,
            probes=set(probes),
            clock=clock,
            audit=self.audit,
            tracer=self.tracer,
        )
        self.verifier = core.Verifier(
            verifier=f"svc:verifier@{cell}",
            key=key,
            probes=probes,
            state_dir=state_dir / "verifier",
            clock=clock,
            audit=self.audit,
            tracer=self.tracer,
        )

    def request(
        self,
        action_type: str,
        target: str,
        *,
        requested_by: int,
        message_id: Any,
        private_chat: bool = True,
    ) -> dict[str, Any]:
        """The owner asked (TOTP checked) for an action: propose, decide, execute. Returns the
        ExecutionRecord: status ``started`` (the host helper has the request) or ``rejected``."""
        with self.tracer.span(
            "telegram.command", **{"autonomy.action_type": action_type, "autonomy.target": target}
        ):
            return self._request(
                action_type,
                target,
                requested_by=requested_by,
                message_id=message_id,
                private_chat=private_chat,
            )

    def _request(
        self,
        action_type: str,
        target: str,
        *,
        requested_by: int,
        message_id: Any,
        private_chat: bool,
    ) -> dict[str, Any]:
        now = self.clock()
        _, risk = ACTIONS[action_type]
        proposal: dict[str, Any] = {
            "proposal_id": core.prefixed_id("prp", now),
            "proposer": "owner:ops",
            "objective": {
                "id": "obj_owner_command",
                "text": f"{action_type} {target} requested in Telegram",
            },
            "action": {"type": action_type, "target": target, "params": {}},
            "rationale": f"owner command from Telegram user {requested_by}, authenticator code checked",
            "inputs": [],
            **self._expectation(action_type, target),
            "reversibility": {"irreversible": True, "note": "a restart or reboot cannot be undone"},
            "freshness_s": 300,
            "idempotency_key": f"telegram:{requested_by}:{message_id}:{action_type}:{target}",
        }
        corr = f"corr_{proposal['proposal_id'][4:]}"
        evaluation = self.evaluator.evaluate(proposal, risk_class=risk, correlationid=corr)
        req = decision_request(
            proposal,
            cell=self.cell,
            risk_class=risk,
            trust="owner",
            now=now,
            owner_verified=True,
            evaluation=evaluation["outcome"],
        )
        decision = self.pdp.decide(req)
        self.audit.append(
            "action.decided",
            summary=f"{decision['effect']}: {action_type} {target} ({decision.get('reason', '')})"[
                :1000
            ],
            correlationid=f"corr_{proposal['proposal_id'][4:]}",
            refs={"proposal": proposal["proposal_id"], "decision": decision["decision_id"]},
        )
        approval = None
        if decision["effect"] == "require_approval":
            # R2 needs an approval artifact even for the owner: this command carried a checked
            # authenticator code in a private chat, so the approval service attests it now
            try:
                approval = self.approvals.attest(
                    decision, operator=requested_by, private_chat=private_chat, code_ok=True
                )
            except core.Reject as why:
                log.warning("governance: approval refused: %s", why)
        record: dict[str, Any] = self.gateway.execute(
            {
                "action_id": core.prefixed_id("act", now),
                "proposal": proposal,
                "decision": decision,
                "idempotency_key": proposal["idempotency_key"],
                "correlationid": f"corr_{proposal['proposal_id'][4:]}",
                **({"approval": approval} if approval else {}),
            },
            traceparent=core.current_traceparent(),
            correlationid=f"corr_{proposal['proposal_id'][4:]}",
        )
        return record

    def ask(
        self, proposal: dict[str, Any], decision: dict[str, Any], summary: str
    ) -> dict[str, Any]:
        """A pending approval request (for proposals by components, e.g. agents)."""
        q: dict[str, Any] = self.approvals.request(proposal, decision, summary)
        self.audit.append(
            "approval.requested",
            summary=f"{q['request_id']}: {summary}"[:1000],
            correlationid=f"corr_{proposal['proposal_id'][4:]}",
            refs={"proposal": proposal["proposal_id"], "decision": decision["decision_id"]},
        )
        return q

    def submit(self, proposal: dict[str, Any], *, summary: str) -> dict[str, Any]:
        """A proposal by a component (an agent, M7): evaluate, decide, then execute (a grant
        allows it), ask the owner (/pending), or refuse. Returns ``{"status": ...}`` with
        ``request_id`` (pending_approval), ``action_id`` (started) or ``reason`` (denied)."""
        with self.tracer.span(
            "agent.proposal", **{"autonomy.proposer": str(proposal.get("proposer"))}
        ):
            return self._submit(proposal, summary)

    def _submit(self, proposal: dict[str, Any], summary: str) -> dict[str, Any]:
        now = self.clock()
        action = proposal["action"]
        if action["type"] not in ACTIONS:
            return {"status": "denied", "reason": f"{action['type']} is not an action of this node"}
        _, risk = ACTIONS[action["type"]]
        corr = f"corr_{proposal['proposal_id'][4:]}"
        self.audit.append(
            "action.proposed",
            summary=f"{proposal['proposer']}: {action['type']} {action['target']}"[:1000],
            correlationid=corr,
            refs={"proposal": proposal["proposal_id"]},
        )
        evaluation = self.evaluator.evaluate(proposal, risk_class=risk, correlationid=corr)
        trust = (
            "untrusted"
            if any(i.get("trust") == "untrusted" for i in proposal.get("inputs", []))
            else "trusted"
        )
        decision = self.pdp.decide(
            decision_request(
                proposal,
                cell=self.cell,
                risk_class=risk,
                trust=trust,
                now=now,
                evaluation=evaluation["outcome"],
            )
        )
        self.audit.append(
            "action.decided",
            summary=f"{decision['effect']}: {action['type']} {action['target']} ({decision.get('reason', '')})"[
                :1000
            ],
            correlationid=corr,
            refs={"proposal": proposal["proposal_id"], "decision": decision["decision_id"]},
        )
        if decision["effect"] == "require_approval":
            q = self.ask(proposal, decision, summary)
            return {"status": "pending_approval", "request_id": q["request_id"]}
        if decision["effect"] == "deny":
            return {"status": "denied", "reason": str(decision.get("reason", "denied"))}
        record = self.gateway.execute(
            {
                "action_id": core.prefixed_id("act", now),
                "proposal": proposal,
                "decision": decision,
                "idempotency_key": proposal["idempotency_key"],
                "correlationid": corr,
            },
            traceparent=core.current_traceparent(),
            correlationid=corr,
        )
        return {
            "status": record["status"],
            "action_id": record["action_id"],
            "reason": str(record.get("error", "")),
        }

    def withdraw(self, proposer: str) -> int:
        """Withdraw a suspended component's waiting proposals (C11: suspend cancels)."""
        n = 0
        for q in self.approvals.pending():
            if str(q["proposal"].get("proposer", "")).split("@", 1)[0] == proposer.split("@", 1)[0]:
                if self.approvals.deny(q["request_id"]) is not None:
                    n += 1
                    self.audit.append(
                        "approval.withdrawn",
                        summary=f"{q['request_id']} withdrawn: {proposer} was suspended",
                        correlationid=f"corr_{q['proposal']['proposal_id'][4:]}",
                        refs={
                            "proposal": q["proposal"]["proposal_id"],
                            "decision": q["decision"]["decision_id"],
                        },
                    )
        return n

    def use_reviewer(self, models: Any, deadline_s: float = 20.0) -> None:
        """Give the evaluator an LLM reviewer on the model gateway (escalate-only; no model:
        skipped, ST-03)."""
        self.evaluator.reviewer = core.ModelReviewer(
            models, requested_by=f"svc:evaluator@{self.cell}", deadline_s=deadline_s
        )

    def approve(self, request_id: str, *, operator: int, private_chat: bool) -> dict[str, Any]:
        """The owner approved (code checked by the caller): execute through the gateway."""
        q, approval = self.approvals.approve(
            request_id, operator=operator, private_chat=private_chat, code_ok=True
        )
        p, d = q["proposal"], q["decision"]
        return dict(
            self.gateway.execute(
                {
                    "action_id": core.prefixed_id("act", self.clock()),
                    "proposal": p,
                    "decision": d,
                    "approval": approval,
                    "idempotency_key": p["idempotency_key"],
                    "correlationid": f"corr_{p['proposal_id'][4:]}",
                }
            )
        )

    def deny(self, request_id: str, operator: int) -> bool:
        q = self.approvals.deny(request_id)
        if q is None:
            return False
        self.audit.append(
            "approval.denied",
            summary=f"{request_id} denied by Telegram user {operator}",
            correlationid=f"corr_{q['proposal']['proposal_id'][4:]}",
            refs={
                "proposal": q["proposal"]["proposal_id"],
                "decision": q["decision"]["decision_id"],
            },
        )
        return True

    def pending_text(self) -> str:
        pending = self.approvals.pending()
        if not pending:
            return "No approvals waiting."
        lines = [f"{len(pending)} approval(s) waiting:"]
        for q in pending:
            left = max(0, int(q["expires_at"] - self.clock()))
            lines.append(f"{q['request_id']}: {q['summary']} (expires in {left // 60} min)")
        lines.append("Answer with /approve <id> <code> or /deny <id>.")
        return "\n".join(lines)

    def order(self, kind: str, scope: str, reason: str, operator: int) -> dict[str, Any]:
        """suspend / hold / resume, for the owner (checked by the caller); audited."""
        o: dict[str, Any] = self.control.order(kind, scope, f"{reason} (Telegram user {operator})")
        self.audit.append(
            "system.resumed" if kind == "resume" else "system.suspended",
            summary=f"{kind} {o['scope']}: {reason}"[:1000],
            correlationid=f"corr_{o['order_id'][4:]}",
            refs={"order": o["order_id"]},
        )
        return o

    def finished(self, action_id: str | None, ok: bool, detail: str = "") -> None:
        """The host helper's result for an action this gateway started."""
        rec = self.gateway.record(action_id) if action_id else None
        if action_id is None or rec is None or rec.get("status") != "started":
            return
        kind = ACTIONS.get(str(rec_action(self.gateway, action_id)), ("", ""))[0]
        if kind:
            self.jobs[kind] = {
                "value": "ok" if ok else "failed",
                "at": self.clock(),
                "detail": detail[:200],
            }
            try:
                self.jobs_file.write_text(json.dumps(self.jobs))
            except OSError as exc:
                log.warning("governance: cannot keep the job result: %s", exc)
        result = core.AdapterResult(
            "succeeded" if ok else "failed",
            {"detail": detail[:200]} if detail else {},
            error="" if ok else detail[:300],
        )
        self.gateway.complete(action_id, result)

    def pending(self, kind: str) -> str | None:
        """The action id of a started action of this job kind, if any (after a restart)."""
        for aid, entry in self.gateway.started.items():
            if ACTIONS.get(str(entry["action"]["type"]), ("", ""))[0] == kind:
                return str(aid)
        return None

    # -- C13 / C14 (Autonomy M8)

    def _expectation(self, action_type: str, target: str) -> dict[str, Any]:
        """What should be true afterwards, and how this node checks it."""
        kind, _ = ACTIONS[action_type]
        if action_type == "host.reboot" and self.boot_time is not None:
            effect = {"kind": "restarted_after", "entity": "host", "within_s": 1800}
            probe = {"probe": "host.boot_time", "independent": True}
        elif action_type == "host.restart_service" and target == "frigate" and self.frigate_started:
            effect = {"kind": "restarted_after", "entity": "frigate", "within_s": 600}
            probe = {"probe": "frigate.uptime", "independent": True}
        else:
            effect = {
                "kind": "state_equals",
                "entity": f"host.job.{kind}",
                "value": "ok",
                "within_s": 3600,
            }
            probe = {"probe": "host.job_result", "independent": False}
        return {"expected_effect": effect, "verification": probe}

    def _open_proposals(self) -> list[dict[str, Any]]:
        out = []
        for aid, entry in self.gateway.started.items():
            pid = (entry.get("expect") or {}).get("proposal_id") or f"prp_{aid[4:]}"
            out.append({"proposal_id": pid, "action": entry["action"]})
        return out

    def _probe_job(self, effect: Any, action: Any, since: float) -> list[Any]:
        kind = str(effect.get("entity", "")).rsplit(".", 1)[-1]
        job = self.jobs.get(kind)
        if not job or float(job["at"]) < since:
            return []
        return [core.Observation(f"job:{kind}", job["value"], float(job["at"]), "host.job_result")]

    def _probe_boot(self, effect: Any, action: Any, since: float) -> list[Any]:
        assert self.boot_time is not None
        t = self.boot_time()
        if t is None:
            raise OSError("host boot time unavailable")
        return [core.Observation("host:boot_time", t, self.clock(), "host.boot_time")]

    def _probe_frigate(self, effect: Any, action: Any, since: float) -> list[Any]:
        assert self.frigate_started is not None
        t = self.frigate_started()
        if t is None:
            raise OSError("Frigate stats unavailable")
        return [core.Observation("frigate:stats", t, self.clock(), "frigate.uptime")]

    def verify_pending(self, final: bool = False) -> list[dict[str, Any]]:
        """Check every executed action still waiting for verification; returns the new
        verifications (contradicted ones also opened an incident in the audit log)."""
        return list(self.verifier.check_pending(self.gateway, final=final))

    def verification_text(self, v: dict[str, Any]) -> str:
        entry = self.gateway.entries.get(v["action_id"], {})
        a = entry.get("action", {})
        what = f"{a.get('type', 'action')} {a.get('target', '')}".strip()
        if v["result"] == "confirmed":
            return f"Verified: {what} took effect."
        if v["result"] == "contradicted":
            return f"Warning: {what} was reported done but did not take effect ({v.get('note', '')}). Incident recorded; see /audit."
        if v["result"] == "not_confirmed":
            return f"Could not confirm that {what} took effect ({v.get('note', '')})."
        return f"Could not check {what}: {v.get('note', 'inconclusive')}."

    def audit_text(self, n: int = 10) -> str:
        records = list(self.audit.records())
        problems = core.verify_chain(records, self.keys)
        lines = [
            f"Audit log of {self.cell}: {len(records)} record(s), chain {'intact' if not problems else 'BROKEN'}"
        ]
        if problems:
            lines += [f"  {p}" for p in problems[:3]]
        if self.bundle_problem:
            lines.append(f"  WARNING: {self.bundle_problem}")
        lines.append(f"  policy bundle {self.pdp.bundle.version}")
        for o in self.control.active():
            lines.append(
                f"  ACTIVE: {o['kind']} {o['scope']} since {o['issued_at']}: {o['reason']}"
            )
        for rec in records[-n:]:
            trace = f" [trace {rec['trace_id'][:8]}]" if rec.get("trace_id") else ""
            lines.append(f"{rec['time']} {rec['kind']}: {rec['summary']}{trace}")
        return "\n".join(lines)


def rec_action(gateway: ActionGateway, action_id: str) -> str:
    entry = gateway.entries.get(action_id) or {}
    return str((entry.get("action") or {}).get("type", ""))


def _known_service(services: Callable[[], list[str]]) -> Callable[[Any], tuple[bool, str]]:
    def check(proposal: Any) -> tuple[bool, str]:
        target = str(proposal["action"]["target"])
        known = services()
        if target in known:
            return True, ""
        return (
            False,
            f"{target} is not a service of this stack ({', '.join(known) or 'none found'})",
        )

    return check
