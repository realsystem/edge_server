#!/usr/bin/env python3
"""frigate-stub: just enough of Frigate's HTTP API for the node's bot (lab only).

GET /api/version, /api/stats, /api/config as the bot reads them. The uptime is the
process's own, so `docker restart frigate` resets it, like the real thing.
Admin: POST /_lab/camera {"name": "cam1_front", "fps": 0}  (0 = "NO VIDEO"),
       POST /_lab/camera {"name": "cam1_front", "enabled": false}
Standard library only.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.time()
LOCK = threading.Lock()
CAMERAS = {n: {"fps": 5.0, "enabled": True} for n in ("cam1_front", "cam2_rear", "cam3_side", "cam4_gate")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, raw=False):
        data = body.encode() if raw else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if raw else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/version":
            return self._send(200, "0.0.0-lab-stub", raw=True)
        if self.path == "/api/stats":
            with LOCK:
                cams = {
                    n: {"camera_fps": c["fps"] if c["enabled"] else 0.0, "detection_fps": 1.0 if c["fps"] else 0.0,
                        "skipped_fps": 0.0}
                    for n, c in CAMERAS.items()
                }
            return self._send(200, {"service": {"uptime": int(time.time() - START), "version": "0.0.0-lab-stub"},
                                    "detectors": {"cpu": {"inference_speed": 12.0}}, "cameras": cams})
        if self.path == "/api/config":
            with LOCK:
                return self._send(200, {"cameras": {n: {"enabled": c["enabled"]} for n, c in CAMERAS.items()}})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/_lab/camera":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            name = body["name"]
            with LOCK:
                cam = CAMERAS[name]
                if "fps" in body:
                    cam["fps"] = float(body["fps"])
                if "enabled" in body:
                    cam["enabled"] = bool(body["enabled"])
            self._send(200, {"ok": True})
        except (KeyError, ValueError, TypeError):
            self._send(400, {"error": "need name (and fps or enabled)"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "5000"))), Handler).serve_forever()
