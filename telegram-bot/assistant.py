"""/ask: a node assistant built on Autonomy M7 (C12 model gateway, C11 agent host).

The assistant reads this node through three tools (status, audit log, Frigate cameras),
asks a language model through the model gateway, and answers in Telegram. The model runs
where the routing says: by default an ``llm.chat`` job on the job processor
(``INSTALL_PROCESSOR``), i.e. the GPU machine at home; ``MODELS_CONFIG`` can point to a
routing file instead (autonomy ``ops/models.example.yaml``). Node data is classed ``site``:
it may go to the owner's machines, never to a cloud model.

The assistant cannot act. If it thinks restarting one service would help, it *proposes*
that restart through governance (evaluator → policy → approval → gateway). With the default
policy it has no grant, so the proposal is denied and the answer says which /restart to
run; with an owner grant for ``agent:node-assistant`` (autonomy level A1) the proposal
waits in /pending for /approve.

``MODEL_REVIEW=true`` also gives governance's evaluator an LLM reviewer (escalate-only;
without a model the review is skipped, ST-03).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import autonomy_core as core

log = logging.getLogger("edge.assistant")

AGENT_ID = "agent:node-assistant"

MANIFEST: dict[str, Any] = {
    "component_id": AGENT_ID,
    "version": "0.1.0",
    "kind": "agent",
    "description": "Answers the owner's questions about this edge server and may propose a service restart.",
    "contracts": {
        "envelope": "^0.1",
        "health": "^0.1",
        "action-lifecycle": "^0.1",
        "audit": "^0.1",
    },
    "capabilities": {
        "requires": [{"name": "model.chat", "version": "^1", "privacy": "cloud_ok"}],
        "proposes_actions": ["host.restart_service"],
    },
    "trust_class": "trusted-input",
    "runtime": {"packaging": ["library"]},
    "agent": {
        "tools": ["node.status", "node.audit", "frigate.cameras"],
        "budgets": {"model_calls_per_day": 100, "proposals_per_day": 5, "run_seconds": 180},
    },
}

SYSTEM = (
    "You are the assistant of an edge server that records security cameras with Frigate and "
    "reports to its owner on Telegram. Answer the owner's question from the data you are given; "
    "say plainly when the data does not tell. At most 8 short sentences, no markdown. If "
    "restarting exactly one service would likely fix the problem, put its name in restart; "
    "otherwise restart is an empty string. Never invent services."
)

ANSWER_SCHEMA = {
    "type": "object",
    "required": ["answer", "restart"],
    "properties": {
        "answer": {"type": "string", "maxLength": 2000},
        "restart": {"type": "string", "maxLength": 64},
    },
}


class NodeModels:
    """The model gateway for this node, rebuilt when its config or API key changes (the
    deploy writes the processor key after the bot has started)."""

    def __init__(
        self,
        *,
        processor_url: str = "",
        key_file: Path | None = None,
        key: str = "",
        model: str = "",
        config_file: Path | None = None,
        usage_path: Path | None = None,
        tracer: Any = None,
    ):
        self.processor_url = processor_url
        self.key_file = key_file
        self.key = key
        self.model = model
        self.config_file = config_file
        self.usage_path = usage_path
        self.tracer = tracer
        self.adapters: dict[str, Any] = dict(core.models.ADAPTERS)  # tests swap in fakes
        self._gw: core.ModelGateway | None = None
        self._stamp: tuple[str, str] | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.config_file or self.processor_url)

    def _current_key(self) -> str:
        if self.key:
            return self.key
        if self.key_file is not None:
            try:
                return self.key_file.read_text().strip()
            except OSError:
                return ""
        return ""

    def gateway(self) -> core.ModelGateway | None:
        if self.config_file is not None:
            try:
                text = self.config_file.read_text()
            except OSError as e:
                log.warning("MODELS_CONFIG %s: %s", self.config_file, e)
                return None
            stamp = ("config", text)
        elif self.processor_url:
            stamp = ("processor", self._current_key())
            if not stamp[1]:
                return None
        else:
            return None
        with self._lock:
            if stamp != self._stamp:
                self._gw = self._build(stamp)
                self._stamp = stamp
            return self._gw

    def _build(self, stamp: tuple[str, str]) -> core.ModelGateway | None:
        if stamp[0] == "config":
            try:
                import yaml

                doc = yaml.safe_load(stamp[1])
            except Exception:  # noqa: BLE001 - JSON is YAML too, but yaml may be missing
                import json

                doc = json.loads(stamp[1])
        else:
            doc = {
                "endpoints": {
                    "home-gpu": {
                        "kind": "processor",
                        "url": self.processor_url,
                        "locality": "owned",
                        "model": self.model,
                        "api_key": stamp[1],
                        "priority": 10,
                        "retention_s": 3600,
                    }
                },
                "routes": {"*": ["home-gpu"]},
            }
        try:
            return core.ModelGateway.from_config(
                doc, usage_path=self.usage_path, adapters=self.adapters, tracer=self.tracer
            )
        except (ValueError, TypeError) as e:
            log.warning("model routing config refused: %s", e)
            return None

    # what AgentHost and ModelReviewer use
    def complete(self, request: Mapping[str, Any], cancel: threading.Event | None = None) -> Any:
        gw = self.gateway()
        if gw is None:
            raise core.ModelUnavailable("unavailable.model", "no model configured on this node yet")
        return gw.complete(request, cancel=cancel)

    def spent(self, who: str) -> tuple[int, int]:
        gw = self.gateway()
        return gw.spent(who) if gw is not None else (0, 0)


class Assistant:
    def __init__(
        self,
        models: NodeModels,
        *,
        tools: Mapping[str, Callable[..., Any]],
        services: Callable[[], list[str]],
        governance: Any = None,
        cell: str = "local",
        clock: Callable[[], float],
    ):
        self.models = models
        self.services = services
        self.governance = governance
        self.host = core.AgentHost(
            gateway=models,  # type: ignore[arg-type]  # duck-typed: complete() and spent()
            tools=tools,
            submit=self._submit,
            withdraw=self._withdraw,
            suspended=self._suspended,
            cell=cell,
            clock=clock,
            audit=getattr(governance, "audit", None),
            tracer=getattr(governance, "tracer", None),
        )
        self.host.register(MANIFEST, self._run)

    def _suspended(self, agent: str) -> bool:
        control = getattr(self.governance, "control", None)
        return control is not None and control.blocked(agent) is not None

    def _withdraw(self, agent: str) -> None:
        if self.governance is not None:
            self.governance.withdraw(agent)

    def _submit(self, proposal: dict[str, Any]) -> dict[str, Any]:
        if self.governance is None:
            return {"status": "not_submitted", "reason": "governed actions are off on this node"}
        return dict(
            self.governance.submit(
                proposal, summary=f"restart {proposal['action']['target']} (assistant)"
            )
        )

    def _run(self, ctx: core.AgentContext) -> dict[str, Any]:
        question = str(ctx.trigger["question"])
        status = ctx.tool("node.status")
        audit = ctx.tool("node.audit")
        cameras = ctx.tool("frigate.cameras")
        answer = ctx.ask(
            "ask.node",
            [
                ctx.as_message(status, "Node status"),
                ctx.as_message(audit, "Recent audit log"),
                ctx.as_message(cameras, "Frigate cameras"),
                {
                    "role": "user",
                    "content": f"Services you may name: {', '.join(self.services()) or 'none'}.\n"
                    f"Question: {question}",
                },
            ],
            system=SYSTEM,
            output_schema=ANSWER_SCHEMA,
            privacy="site",
            max_tokens=500,
        )
        data = (
            answer.data
            if isinstance(answer.data, dict)
            else {"answer": answer.content, "restart": ""}
        )
        out: dict[str, Any] = {
            "answer": str(data.get("answer", "")).strip(),
            "via": f"{answer.endpoint} ({answer.model})",
        }
        service = str(data.get("restart") or "").strip()
        if service and service in self.services():
            out["restart"] = service
            out["proposal"] = ctx.propose(
                "host.restart_service",
                service,
                {},
                rationale=out["answer"][:2000],
                inputs=[status, cameras],
                objective=question,
                expected_effect={
                    "kind": "state_equals",
                    "entity": "host.job.restart",
                    "value": "ok",
                    "within_s": 3600,
                },
                verification={"probe": "host.job_result", "independent": False},
                reversibility={"irreversible": True},
                freshness_s=600,
            )
        return out

    def ask(self, question: str) -> str:
        """The answer text for Telegram (never raises)."""
        result = self.host.run(AGENT_ID, {"question": question})
        if result.status == "suspended":
            return "The assistant is suspended (/resume to let it run again)."
        if result.status == "budget_exceeded":
            return "The assistant has used its model budget for today."
        if result.status == "stopped":
            return "No answer in time (3 minutes). Is the GPU machine on and its worker running?"
        if result.status == "error":
            err = result.error
            if "policy.model_refused" in err:
                return "No model may see this node's data with the current routing (MODELS_CONFIG)."
            if "unavailable.model" in err or "budget.exceeded" in err:
                return (
                    "No model answered. Check that the GPU machine is on and its processor worker "
                    f"is online (/status). ({err[:200]})"
                )
            return f"The assistant failed: {err[:300]}"
        out = result.output or {}
        text = out.get("answer") or "(no answer)"
        if out.get("restart"):
            p = out.get("proposal") or {}
            if p.get("status") == "pending_approval":
                text += (
                    f"\n\nI proposed restarting {out['restart']}: it waits for you as "
                    f"{p.get('request_id')} (/pending, /approve {p.get('request_id')} <code>)."
                )
            elif p.get("status") in ("started", "succeeded"):
                text += f"\n\nI restarted {out['restart']} (allowed by your grant); see /audit."
            else:
                text += (
                    f"\n\nRestarting {out['restart']} may help. I may not propose it myself "
                    f"({p.get('reason', 'no grant')}); run /restart {out['restart']} <code> if you agree."
                )
        return text + f"\n\n— {out.get('via', 'model')}"
