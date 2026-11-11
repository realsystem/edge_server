"""The lab (lab/): stand-ins are tested directly; the render step runs the production deploy
script against stubs, as the other deploy tests do. No Docker daemon needed."""

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAB = REPO / "lab"
sys.path.insert(0, str(LAB / "frigate-stub"))
sys.path.insert(0, str(LAB / "telegram-mock"))


def load(name, path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


frigate = load("frigate_stub", LAB / "frigate-stub/server.py")
tgmock = load("telegram_mock", LAB / "telegram-mock/server.py")


def serve(mod):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def post(url, body):
    r = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=10))


def get(url):
    return urllib.request.urlopen(url, timeout=10).read().decode()


class TestFrigateStub:
    def test_api_as_the_bot_reads_it(self):
        httpd, base = serve(frigate)
        try:
            assert "lab-stub" in get(base + "/api/version")
            stats = json.loads(get(base + "/api/stats"))
            assert stats["service"]["uptime"] >= 0 and len(stats["cameras"]) == 4
            post(base + "/_lab/camera", {"name": "cam1_front", "fps": 0})
            stats = json.loads(get(base + "/api/stats"))
            assert stats["cameras"]["cam1_front"]["camera_fps"] == 0
            post(base + "/_lab/camera", {"name": "cam1_front", "enabled": False})
            assert (
                json.loads(get(base + "/api/config"))["cameras"]["cam1_front"]["enabled"] is False
            )
        finally:
            httpd.shutdown()


class TestTelegramMock:
    def test_message_round_trip(self):
        httpd, base = serve(tgmock)
        try:
            post(base + "/_lab/say", {"text": "/status", "user_id": 1001})
            got = post(base + "/bot1:tok/getUpdates", {"timeout": 1, "offset": 0})
            update = got["result"][0]
            assert (
                update["message"]["text"] == "/status" and update["message"]["chat"]["id"] == 1001
            )
            # offset acknowledges it
            assert (
                post(
                    base + "/bot1:tok/getUpdates",
                    {"timeout": 0, "offset": update["update_id"] + 1},
                )["result"]
                == []
            )
            # an acknowledged update is gone for a bot that restarts and asks again from 0
            assert post(base + "/bot1:tok/getUpdates", {"timeout": 0, "offset": 0})["result"] == []
            post(base + "/bot1:tok/sendMessage", {"chat_id": 1001, "text": "hello"})
            assert json.loads(get(base + "/_lab/sent"))["messages"][-1]["text"] == "hello"
        finally:
            httpd.shutdown()


@pytest.fixture
def rendered(tmp_path):
    app = tmp_path / "app"
    cred = tmp_path / "telegram"
    cred.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:labmocktokenaaaaaaaaaaaaaaaaaaaaaaa\n"
        "TELEGRAM_ALLOWED_IDS=1001\nREBOOT_TOTP_SECRET=JBSWY3DPEHPK3PXPJBSWY3DP\n"
    )
    env = {**os.environ, "APP": str(app), "REPO": str(REPO), "CRED": str(cred)}
    result = subprocess.run(
        ["bash", str(LAB / "render.sh")], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return app


class TestRender:
    def test_production_script_makes_the_node_stack(self, rendered):
        import yaml

        services = yaml.safe_load((rendered / "docker-compose.yml").read_text())["services"]
        assert set(services) == {"frigate", "telegram-bot"}
        bot = services["telegram-bot"]
        assert "./bot-requests:/requests" in bot["volumes"]  # the drop box host-stub watches
        assert (rendered / "bot-requests").is_dir() and (rendered / "bot-state").is_dir()

    @pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
    def test_lab_override_merges(self, rendered, tmp_path):
        env = {**os.environ, "LAB_STATE": str(tmp_path / "state"), "LAB_EDGE": str(REPO)}
        r = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(rendered),
                "-f",
                str(rendered / "docker-compose.yml"),
                "-f",
                str(LAB / "compose.lab.yml"),
                "--profile",
                "mock-telegram",
                "config",
                "--format",
                "json",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0 and "unknown flag" in r.stderr:
            pytest.skip("docker compose too old")
        assert r.returncode == 0, r.stderr
        cfg = json.loads(r.stdout)
        assert set(cfg["services"]) == {"frigate", "telegram-bot", "host-stub", "telegram-mock"}
        frigate_svc = cfg["services"]["frigate"]
        assert (
            not frigate_svc.get("privileged")
            and not frigate_svc.get("devices")
            and not frigate_svc.get("ports")
        )
        assert list(cfg["services"]["telegram-bot"]["networks"]) == ["lab"]


class TestShell:
    def test_lab_scripts_parse(self):
        for f in ("lab.sh", "render.sh"):
            assert subprocess.run(["bash", "-n", str(LAB / f)]).returncode == 0
