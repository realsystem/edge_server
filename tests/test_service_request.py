"""telegram-bot/service-request.py: the host side of the bot's /restart and /logs."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "telegram-bot" / "service-request.py"

spec = importlib.util.spec_from_file_location("service_request", SCRIPT)
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)

SERVICES = ["frigate", "mosquitto", "telegram-bot"]
LEAK_URL_PASSWORD = "uRl" + "-pass-1"
LEAK_KEYVALUE = "hunter" + "2hunter2"
LEAK_CAM = "zebra" + "-pw-9"
LEAK_TOKEN = "123456789:" + "A" * 35


@pytest.fixture
def app(tmp_path, monkeypatch):
    (tmp_path / "bot-requests").mkdir()
    (tmp_path / ".cam").write_text(f"192.168.1.20 admin:{LEAK_CAM}\n")
    monkeypatch.setenv("EDGE_SERVER_DIR", str(tmp_path))
    monkeypatch.setattr(sr.time, "sleep", lambda s: None)
    return tmp_path


class FakeDocker:
    """Stands in for sr.run: records the docker calls and answers them."""

    def __init__(self, services=SERVICES):
        self.calls = []
        self.services = services
        self.restart_rc = 0
        self.state = "running false"
        self.logs = "frigate | started\n"
        self.logs_rc = 0

    def __call__(self, args, timeout):
        self.calls.append(args)
        out, rc, err = "", 0, ""
        if args[:3] == ["docker", "compose", "config"]:
            out = "\n".join(self.services) + "\n"
        elif args[:3] == ["docker", "compose", "restart"]:
            rc, err = self.restart_rc, "Error response from daemon: no such container"
        elif args[:3] == ["docker", "compose", "ps"]:
            out = "abc123\n"
        elif args[:2] == ["docker", "inspect"]:
            out = self.state + "\n"
        elif args[:3] == ["docker", "compose", "logs"]:
            out, rc, err = self.logs, self.logs_rc, "service has no logs" if self.logs_rc else ""
        return subprocess.CompletedProcess(args, rc, out, err)

    def restarts(self):
        return [c for c in self.calls if c[:3] == ["docker", "compose", "restart"]]

    def log_calls(self):
        return [c for c in self.calls if c[:3] == ["docker", "compose", "logs"]]


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(sr, "run", fake)
    monkeypatch.setattr(sr, "frigate_answers", lambda: True)
    return fake


def ask(app, kind, payload):
    path = app / "bot-requests" / f"{kind}-request"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def result(app, kind):
    return json.loads((app / "bot-requests" / f"{kind}-result").read_text())


# ------------------------------------------------------------------ restart


def test_restart_ok(app, docker):
    ask(app, "restart", {"service": "mosquitto"})
    assert sr.main(["restart"]) == 0
    assert docker.restarts() == [["docker", "compose", "restart", "-t", "30", "mosquitto"]]
    assert result(app, "restart")["status"] == "ok"
    assert result(app, "restart")["service"] == "mosquitto"
    assert not (app / "bot-requests" / "restart-request").exists()  # consumed


def test_frigate_must_answer_on_its_api(app, docker, monkeypatch):
    monkeypatch.setattr(sr, "frigate_answers", lambda: False)
    monkeypatch.setattr(sr, "RESTART_WAIT", 0.05)
    monkeypatch.setattr(sr, "POLL", 0.0)
    ask(app, "restart", {"service": "frigate"})
    assert sr.main(["restart"]) == 0
    res = result(app, "restart")
    assert res["status"] == "ok" and "API is not answering yet" in res["detail"]


def test_restart_that_leaves_the_container_down_is_a_failure(app, docker, monkeypatch):
    docker.state = "exited false"
    monkeypatch.setattr(sr, "RESTART_WAIT", 0.05)
    monkeypatch.setattr(sr, "POLL", 0.0)
    ask(app, "restart", {"service": "mosquitto"})
    assert sr.main(["restart"]) == 1
    res = result(app, "restart")
    assert res["status"] == "failed" and "exited" in res["detail"]


def test_docker_error_is_reported(app, docker):
    docker.restart_rc = 1
    ask(app, "restart", {"service": "mosquitto"})
    assert sr.main(["restart"]) == 1
    assert "no such container" in result(app, "restart")["detail"]


def test_the_bots_own_container_is_never_restarted(app, docker):
    ask(app, "restart", {"service": "telegram-bot"})
    assert sr.main(["restart"]) == 1
    assert result(app, "restart")["status"] == "refused"
    assert docker.restarts() == []


@pytest.mark.parametrize(
    "name",
    [
        "nginx",
        "Frigate",
        "frigate;id",
        "frigate frigate",
        "../etc",
        "-f",
        "",
        "a" * 60,
        7,
        None,
        ["frigate"],
    ],
)
def test_only_services_of_the_stack_are_accepted(app, docker, name):
    ask(app, "restart", {"service": name})
    assert sr.main(["restart"]) == 1
    assert result(app, "restart")["status"] == "refused"
    assert docker.restarts() == []  # docker was never asked to restart anything


@pytest.mark.parametrize(
    "payload", ["not json", "[1, 2]", '"frigate"', "{" * 10, "x" * 10000, json.dumps({})]
)
def test_malformed_requests_are_refused_and_consumed(app, docker, payload):
    ask(app, "restart", payload)
    assert sr.main(["restart"]) == 1
    assert result(app, "restart")["status"] == "refused"
    assert not (app / "bot-requests" / "restart-request").exists()
    assert docker.restarts() == []


def test_a_missing_request_is_refused(app, docker):
    assert sr.main(["restart"]) == 1
    assert result(app, "restart")["status"] == "refused"


def test_unexpected_errors_still_answer_the_bot(app, monkeypatch):
    def boom(args, timeout):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sr, "run", boom)
    ask(app, "restart", {"service": "frigate"})
    assert sr.main(["restart"]) == 1
    res = result(app, "restart")
    assert res["status"] == "failed" and "FileNotFoundError" in res["detail"]


# --------------------------------------------------------------------- logs


def test_logs_ok_and_the_bots_own_log_is_allowed(app, docker):
    ask(app, "logs", {"service": "telegram-bot", "lines": 40})
    assert sr.main(["logs"]) == 0
    assert docker.log_calls() == [
        ["docker", "compose", "logs", "--no-color", "--timestamps", "--tail", "40", "telegram-bot"]
    ]
    res = result(app, "logs")
    assert res["status"] == "ok" and res["lines"] == 40 and res["service"] == "telegram-bot"
    assert (app / "bot-requests" / "logs-output.txt").read_text() == "frigate | started\n"


@pytest.mark.parametrize(
    "given,used", [(5000, 1000), (0, 1), (-3, 1), ("12", 12), ("many", 100), (None, 100)]
)
def test_line_count_is_clamped(app, docker, given, used):
    ask(app, "logs", {"service": "frigate", "lines": given})
    assert sr.main(["logs"]) == 0
    assert docker.log_calls()[0][docker.log_calls()[0].index("--tail") + 1] == str(used)


def test_logs_for_unknown_service_are_refused(app, docker):
    ask(app, "logs", {"service": "cron"})
    assert sr.main(["logs"]) == 1
    assert result(app, "logs")["status"] == "refused"
    assert docker.log_calls() == []
    assert not (app / "bot-requests" / "logs-output.txt").exists()


def test_passwords_never_leave_the_host(app, docker):
    docker.logs = textwrap.dedent(f"""\
        frigate | connecting rtsp://admin:{LEAK_URL_PASSWORD}@192.168.1.20:554/main
        frigate | mqtt password={LEAK_KEYVALUE} user=x
        frigate | camera login failed for admin / {LEAK_CAM}
        bot | https://api.telegram.org/bot{LEAK_TOKEN}/getUpdates
        frigate | ordinary line stays
        """)
    ask(app, "logs", {"service": "frigate"})
    assert sr.main(["logs"]) == 0
    text = (app / "bot-requests" / "logs-output.txt").read_text()
    for secret in (LEAK_URL_PASSWORD, LEAK_KEYVALUE, LEAK_CAM, LEAK_TOKEN):
        assert secret not in text
    assert "ordinary line stays" in text and "192.168.1.20:554/main" in text
    assert text.count("***") >= 4


def test_huge_logs_keep_the_newest_part(app, docker, monkeypatch):
    monkeypatch.setattr(sr, "MAX_LOG_BYTES", 100)
    docker.logs = "old line\n" * 50 + "the newest line\n"
    ask(app, "logs", {"service": "frigate"})
    assert sr.main(["logs"]) == 0
    text = (app / "bot-requests" / "logs-output.txt").read_text()
    assert len(text.encode()) <= 100 and text.endswith("the newest line\n")


def test_docker_failure_for_logs(app, docker):
    docker.logs_rc = 1
    ask(app, "logs", {"service": "frigate"})
    assert sr.main(["logs"]) == 1
    assert result(app, "logs")["status"] == "failed"


def test_files_are_readable_by_the_bot_and_leave_no_temp_files(app, docker):
    ask(app, "logs", {"service": "frigate"})
    sr.main(["logs"])
    folder = app / "bot-requests"
    for name in ("logs-result", "logs-output.txt"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o644
    assert sorted(p.name for p in folder.iterdir()) == ["logs-output.txt", "logs-result"]


# ------------------------------------------------------------------- script


def test_usage_error():
    assert sr.main([]) == 2
    assert sr.main(["reboot"]) == 2
    assert sr.main(["restart", "extra"]) == 2


def test_redact_helpers():
    assert sr.redact("see https://u:p@host/x") == "see https://***@host/x"
    assert sr.redact('API_KEY: "abc123"') == 'API_KEY: "***"'
    assert sr.redact("nothing here") == "nothing here"


def test_cam_passwords_cover_the_quoted_forms(tmp_path):
    cam = tmp_path / ".cam"
    cam.write_text("# comment\n192.168.1.20 admin:p@ss {w}\nviewer:other-pw\nshort:abc\n")
    found = sr.cam_passwords(cam)
    assert "p@ss {w}" in found and "p%40ss%20%7Bw%7D" in found and "p@ss {{w}}" in found
    assert (
        "other-pw" in found and "abc" not in found
    )  # under 4 characters: too likely to match noise
    assert sr.cam_passwords(tmp_path / "missing") == set()


def test_end_to_end_with_a_docker_on_the_path(tmp_path):
    """The real script as systemd runs it: a fake docker, a request file, a result file."""
    (tmp_path / "bot-requests").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  "compose config --services") printf "frigate\\nmosquitto\\n" ;;\n'
        '  "compose logs"*) echo "mosquitto | hello password=sekrit1234" ;;\n'
        "esac\n"
    )
    docker.chmod(0o755)
    (tmp_path / "bot-requests" / "logs-request").write_text(
        json.dumps({"service": "mosquitto", "lines": 5})
    )
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "logs"],
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "EDGE_SERVER_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads((tmp_path / "bot-requests" / "logs-result").read_text())["status"] == "ok"
    out = (tmp_path / "bot-requests" / "logs-output.txt").read_text()
    assert "hello" in out and "sekrit1234" not in out
