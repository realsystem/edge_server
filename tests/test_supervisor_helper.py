"""ha-addon/supervisor-helper.py: the bot's /restart, /logs and /reboot through the Supervisor."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "supervisor_helper", REPO / "ha-addon/supervisor-helper.py"
)
assert spec and spec.loader
sh = importlib.util.module_from_spec(spec)
sys.modules["supervisor_helper"] = sh
spec.loader.exec_module(sh)


class FakeSupervisor:
    def __init__(self):
        self.calls = []
        self.answers = {}  # (method, path) -> (status, text) or a list of them
        self.down = False

    def call(self, method, path, timeout=30.0):
        self.calls.append((method, path))
        if self.down:
            raise OSError("the Supervisor did not answer")
        a = self.answers.get((method, path), (200, ""))
        if isinstance(a, list):
            a = a.pop(0) if len(a) > 1 else a[0]
        return a


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGE_SERVER_DIR", str(tmp_path))
    (tmp_path / "bot-requests").mkdir()
    host = sh.load_host_helper()
    sup = FakeSupervisor()
    t = [0.0]

    def sleep(s):
        t[0] += s

    helper = sh.Helper(host, sup, ["core", "core_mosquitto"], sleep=sleep, clock=lambda: t[0])
    return helper, sup, tmp_path / "bot-requests"


def result(req, kind):
    return json.loads((req / f"{kind}-result").read_text())


def test_core_restart_waits_for_the_api_and_reports_ok(env):
    helper, sup, req = env
    sup.answers[("GET", "/core/api/")] = [(502, ""), (200, '{"message": "API running."}')]
    (req / "restart-request").write_text('{"service": "core"}')
    helper.once()
    assert ("POST", "/core/restart") in sup.calls
    assert result(req, "restart")["status"] == "ok" and result(req, "restart")["service"] == "core"
    assert not (req / "restart-request").exists()  # consumed


def test_addon_restart_uses_its_slug_and_state(env):
    helper, sup, req = env
    sup.answers[("GET", "/addons/core_mosquitto/info")] = (200, '{"data": {"state": "started"}}')
    (req / "restart-request").write_text('{"service": "core_mosquitto"}')
    helper.once()
    assert ("POST", "/addons/core_mosquitto/restart") in sup.calls
    assert result(req, "restart")["status"] == "ok"


@pytest.mark.parametrize(
    "body", ['{"service": "ssh"}', '{"service": "../x"}', '{"service": 3}', "not json", "[]"]
)
def test_a_name_that_is_not_listed_is_refused_and_nothing_is_called(env, body):
    helper, sup, req = env
    (req / "restart-request").write_text(body)
    helper.once()
    assert result(req, "restart")["status"] == "refused" and sup.calls == []


def test_a_restart_that_never_comes_back_fails(env):
    helper, sup, req = env
    sup.answers[("GET", "/core/api/")] = (502, "")
    (req / "restart-request").write_text('{"service": "core"}')
    helper.once()
    assert result(req, "restart")["status"] == "failed"
    assert "did not come back" in result(req, "restart")["detail"]


def test_a_supervisor_error_or_outage_is_reported(env):
    helper, sup, req = env
    sup.answers[("POST", "/core/restart")] = (403, "")
    (req / "restart-request").write_text('{"service": "core"}')
    helper.once()
    assert "answered 403" in result(req, "restart")["detail"]
    sup.down = True
    (req / "restart-request").write_text('{"service": "core"}')
    helper.once()
    assert result(req, "restart")["status"] == "failed"


def test_logs_are_trimmed_redacted_and_written(env):
    helper, sup, req = env
    text = "".join(f"line {i}\n" for i in range(10)) + "password=hunter2\n"
    sup.answers[("GET", "/core/logs")] = (200, text)
    (req / "logs-request").write_text('{"service": "core", "lines": 3}')
    helper.once()
    out = (req / "logs-output.txt").read_text()
    assert "hunter2" not in out and len(out.splitlines()) == 3
    assert result(req, "logs")["status"] == "ok" and result(req, "logs")["lines"] == 3


def test_logs_lines_are_clamped_and_an_unknown_service_refused(env):
    helper, sup, req = env
    sup.answers[("GET", "/core/logs")] = (200, "a\n")
    (req / "logs-request").write_text('{"service": "core", "lines": 99999}')
    helper.once()
    assert result(req, "logs")["lines"] == 1000
    (req / "logs-request").write_text('{"service": "nope"}')
    helper.once()
    assert result(req, "logs")["status"] == "refused"


def test_reboot_marker_is_consumed_and_the_host_asked_once(env):
    helper, sup, req = env
    (req / "reboot").write_text("")
    helper.once()
    helper.once()
    assert sup.calls == [("POST", "/host/reboot")]
    assert not (req / "reboot").exists()


def test_a_symbolic_link_as_a_request_is_not_followed(env, tmp_path):
    helper, sup, req = env
    secret = tmp_path / "secret.json"
    secret.write_text('{"service": "core"}')
    (req / "restart-request").symlink_to(secret)
    helper.once()
    assert sup.calls == [] and secret.exists()
    assert result(req, "restart")["status"] == "refused"


def test_main_needs_the_supervisor_token(monkeypatch, capsys):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert sh.main() == 1
    assert "SUPERVISOR_TOKEN" in capsys.readouterr().err
