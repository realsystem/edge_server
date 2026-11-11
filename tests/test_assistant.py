# ruff: noqa: F811, F401, E402 - pytest fixtures are imported from the other test modules and used as arguments
"""/ask (Autonomy M7): the node assistant on the model gateway (C12) and agent host (C11).
It reads, asks a model, and can only propose; the owner decides."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
core = pytest.importorskip("autonomy_core")
if not hasattr(core, "AgentHost"):
    pytest.skip("autonomy-core without the M7 agent host", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_governance import agent_wants_restart, audit_kinds, governed, req
from test_telegram_bot import ALICE, bot, code_now, frigate, host, make_bot, reboot, send, telegram

KEY = "pk_0123456789abcdef_" + "a" * 40


def with_models(b, fake, **cfg):
    b.cfg = b.cfg.__class__(
        **{
            **b.cfg.__dict__,
            "processor_url": "http://processor:8080",
            "processor_api_key": KEY,
            **cfg,
        }
    )
    b.setup_assistant()
    assert b.assistant is not None
    b.assistant.models.adapters["processor"] = fake
    return b


def ask(b, question):
    send(b, f"/ask {question}")
    for t in b.threads:
        t.join(10)


def answer(text, restart=""):
    return json.dumps({"answer": text, "restart": restart})


def test_ask_answers_from_node_data(governed, telegram):
    fake = core.FakeProvider(
        answer("Load is low and all cameras run at 5 fps."), model="qwen2.5:14b"
    )
    b = with_models(governed, fake)
    ask(b, "how is the node?")
    texts = telegram.texts()
    assert texts[-2].startswith("Thinking")
    assert texts[-1].startswith("Load is low") and "home-gpu (qwen2.5:14b)" in texts[-1]
    (call,) = fake.calls
    assert (
        call["privacy"] == "site"
        and call["requested_by"] == "agent:node-assistant@" + b.governance.cell
    )
    assert call["task_class"] == "ask.node" and "Edge server" in call["messages"][0]["content"]
    assert "Question: how is the node?" in call["messages"][-1]["content"]
    assert "frigate" in call["messages"][-1]["content"]  # the services it may name
    assert audit_kinds(b)[-2:] == ["agent.started", "agent.finished"]


def test_without_a_grant_it_only_suggests(governed, telegram):
    b = with_models(governed, core.FakeProvider(answer("Frigate stopped answering.", "frigate")))
    ask(b, "why no detections?")
    text = telegram.texts()[-1]
    assert "Restarting frigate may help" in text and "run /restart frigate <code>" in text
    kinds = audit_kinds(b)
    assert ["action.proposed", "action.evaluated", "action.decided"] == [
        k for k in kinds if k.startswith("action.")
    ]
    assert not req(b, "restart-request").exists()  # nothing ran


def test_with_an_approvable_policy_it_waits_for_the_owner(governed, telegram):
    agent_wants_restart(governed.governance)  # installs an approvable "ask the owner" policy
    for q in governed.governance.approvals.pending():
        governed.governance.deny(q["request_id"], ALICE)
    b = with_models(governed, core.FakeProvider(answer("Frigate is stuck.", "frigate")))
    ask(b, "fix detections")
    text = telegram.texts()[-1]
    (q,) = b.governance.approvals.pending()
    assert f"waits for you as {q['request_id']}" in text and q["proposal"]["proposer"].startswith(
        "agent:node-assistant"
    )
    send(b, f"/approve {q['request_id']} {code_now()}")
    assert json.loads(req(b, "restart-request").read_text()) == {"service": "frigate"}


def test_unknown_services_are_never_proposed(governed, telegram):
    b = with_models(governed, core.FakeProvider(answer("Try restarting the database.", "postgres")))
    ask(b, "?")
    assert "may help" not in telegram.texts()[-1] and "action.proposed" not in audit_kinds(b)


def test_no_model_answer(governed, telegram):
    b = with_models(
        governed,
        core.FakeProvider(core.AdapterError("timeout", "no worker finished the job in time")),
    )
    ask(b, "hello")
    assert "No model answered" in telegram.texts()[-1]


def test_off_without_a_model_route(governed, telegram):
    governed.setup_assistant()
    assert governed.assistant is None
    ask(governed, "anything")
    assert "/ask is off" in telegram.texts()[-1]
    assert {
        "command": "ask",
        "description": "Ask the assistant about this node",
    } not in governed.commands()
    with_models(governed, core.FakeProvider("x"))
    assert {
        "command": "ask",
        "description": "Ask the assistant about this node",
    } in governed.commands()
    assert "/ask <question>" in governed.help_text()


def test_key_file_written_later_is_picked_up(governed, tmp_path):
    keyfile = tmp_path / "processor.key"
    b = with_models(
        governed,
        core.FakeProvider(answer("ok")),
        processor_api_key="",
        processor_api_key_file=keyfile,
    )
    with pytest.raises(core.ModelUnavailable):
        b.assistant.models.complete(
            {
                "requested_by": "x",
                "task_class": "t",
                "privacy": "site",
                "messages": [{"role": "user", "content": "x"}],
                "deadline_s": 1,
            }
        )
    keyfile.write_text(KEY)
    assert b.assistant.models.gateway() is not None


def test_suspending_the_assistant(governed, telegram):
    b = with_models(governed, core.FakeProvider(answer("never")))
    send(b, "/suspend agent:node-assistant testing")
    ask(b, "hello")
    assert "suspended" in telegram.texts()[-1]
    assert "agent.suspended" in audit_kinds(b)


def test_suspension_withdraws_waiting_proposals(governed):
    q = agent_wants_restart(governed.governance)
    assert governed.governance.withdraw("agent:fleet-doctor") == 1
    assert governed.governance.approvals.pending() == []
    assert "approval.withdrawn" in audit_kinds(governed)
    assert q["request_id"]


def test_models_config_file_routes_and_refuses(governed, telegram, tmp_path):
    cfg = tmp_path / "models.json"
    cfg.write_text(
        json.dumps(
            {
                "endpoints": {
                    "cloud": {
                        "kind": "openai",
                        "url": "https://api.example.com",
                        "locality": "cloud",
                        "model": "m",
                    }
                },
                "routes": {"*": ["cloud"]},
            }
        )
    )
    b = with_models(governed, core.FakeProvider("x"), models_config=cfg)
    ask(b, "hello")
    assert "No model may see this node's data" in telegram.texts()[-1]


def test_model_review_of_governed_actions(governed, telegram):
    fake = core.FakeProvider(
        json.dumps({"verdict": "escalate", "note": "frigate restarted 3 times today"})
    )
    b = with_models(governed, fake, model_review=True)
    send(b, f"/restart frigate {code_now()}")
    evaluated = [r for r in b.governance.audit.records() if r["kind"] == "action.evaluated"][-1]
    assert (
        evaluated["summary"].startswith("escalate")
        and "frigate restarted 3 times" in evaluated["summary"]
    )
    assert json.loads(req(b, "restart-request").read_text()) == {
        "service": "frigate"
    }  # the owner's code approves
    assert fake.calls[0]["task_class"] == "review" and fake.calls[0]["privacy"] == "private"


def test_model_review_skipped_without_a_model(governed, telegram):
    b = with_models(
        governed,
        core.FakeProvider(core.AdapterError("unavailable", "GPU asleep")),
        model_review=True,
    )
    send(b, f"/restart frigate {code_now()}")
    evaluated = [r for r in b.governance.audit.records() if r["kind"] == "action.evaluated"][-1]
    assert evaluated["summary"].startswith("ok:")  # ST-03: skipped, not blocking
