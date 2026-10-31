"""collect-diagnostics.sh: produces a report and never leaks passwords."""

import json
import os
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "collect-diagnostics.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")

# Built at runtime: ps (in the report) shows the command line that started pytest
CAM_PASSWORD = "s3cr" + "#t:pw {x}"
OTHER_SECRET = "hunter" + "2hunter2"
COMMENT = "a comment " + "line"

STATS = {
    "detectors": {"cpu1": {"inference_speed": 87.5, "pid": 123}},
    "cameras": {
        "front": {"camera_fps": 5.0, "process_fps": 5.0, "detection_fps": 1.2, "skipped_fps": 0.0}
    },
    "cpu_usages": {
        "321": {
            "cpu": "88.0",
            "mem": "1.0",
            "cmdline": f"ffmpeg -i rtsp://admin:{CAM_PASSWORD}@192.168.1.20:554/x",
        }
    },
    "gpu_usages": {},
}


@pytest.fixture
def frigate_api():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps(STATS) if self.path == "/api/stats" else "0.16.0"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def report(tmp_path, frigate_api):
    app = tmp_path / "app"
    (app / "frigate").mkdir(parents=True)
    (app / ".cam").write_text(f"192.168.1.20 admin:{CAM_PASSWORD}\n")
    (app / "frigate" / "config.yml").write_text(
        f"# {COMMENT}\n"
        "mqtt:\n  password: " + OTHER_SECRET + "\n"
        "cameras:\n  front:\n    ffmpeg:\n      inputs:\n"
        f'        - path: "rtsp://admin:{CAM_PASSWORD}@192.168.1.20:554/main"\n'
        # the form Frigate/discovery writes: braces doubled
        f'        - path: "rtsp://admin:{CAM_PASSWORD.replace("{", "{{").replace("}", "}}")}'
        '@192.168.1.20:554/sub"\n'
    )
    (app / "docker-compose.yml").write_text(
        f"services:\n  frigate:\n    environment:\n      - FRIGATE_RTSP_PASSWORD={OTHER_SECRET}\n"
    )
    out = tmp_path / "report.txt"
    env = {
        **os.environ,
        "EDGE_SERVER_DIR": str(app),
        "FRIGATE_URL": frigate_api,
        "SAMPLES": "1",
        "INTERVAL": "1",
        "FRIGATE_CONTAINER": "no-such-container",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), str(out)], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return out, result


def test_report_has_the_useful_sections(report):
    out, result = report
    text = out.read_text()
    for heading in ("System", "CPU", "GPU and accelerators", "Docker", "Frigate API"):
        assert f"===== {heading}" in text
    assert "Load: top" in text and "Busiest processes" in text
    # Frigate API summary: the numbers needed to size the detector
    assert "cpu1: inference_speed=87.5" in text
    assert "front: camera_fps=5.0" in text
    assert "version: 0.16.0" in text
    # config is included (structure), comments are not
    assert "cameras:" in text and COMMENT not in text
    assert "Report written to" in result.stderr


def test_report_is_private(report):
    out, _ = report
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_no_passwords_in_report(report):
    out, result = report
    text = out.read_text()
    assert CAM_PASSWORD not in text
    assert OTHER_SECRET not in text
    assert "s3cr" + "#t" not in text
    assert "***" in text
    # Host and path stay readable for debugging
    assert "@192.168.1.20:554/main" in text
    assert CAM_PASSWORD not in result.stdout + result.stderr


def test_missing_tools_and_unreadable_config_do_not_abort(tmp_path):
    out = tmp_path / "r.txt"
    env = {
        **os.environ,
        "EDGE_SERVER_DIR": str(tmp_path / "nothing"),
        "FRIGATE_URL": "http://127.0.0.1:9",
        "SAMPLES": "1",
        "INTERVAL": "1",
        "FRIGATE_CONTAINER": "no-such-container",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), str(out)], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    text = out.read_text()
    assert "cannot read" in text and "unreachable" in text
