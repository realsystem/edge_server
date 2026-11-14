#!/usr/bin/env python3
"""supervisor-stub: the Home Assistant Supervisor (and Core) as far as the add-on bot uses it, for the lab.

Port 80 answers the Supervisor API (needs "Authorization: Bearer lab-token"):
  POST /core/restart, POST /addons/<slug>/restart   recorded; Core answers 502 for a few seconds
  GET  /core/api/                                    200 "API running." (502 while "restarting")
  GET  /addons/<slug>/info                           {"data": {"state": "started"}}
  GET  /core/logs, /addons/<slug>/logs               text with a password in it (the bot must redact it)
  POST /host/reboot                                  recorded; nothing is rebooted
Port 8123 answers like Home Assistant to any GET, for the bot's /status check.
Lab admin: GET /_lab/calls -> [{"method","path"}], POST /_lab/clear. Standard library only.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("SUPERVISOR_TOKEN", "lab-token")
RESTART_SECONDS = float(os.environ.get("RESTART_SECONDS", "4"))
LOCK = threading.Lock()
CALLS = []
STATE = {"core_down_until": 0.0}

LOG = (
    "2026-11-13 20:00:01 INFO (MainThread) [homeassistant.core] Home Assistant starting\n"
    "2026-11-13 20:00:02 INFO (MainThread) [lab] mqtt login password=hunter2 accepted\n"
    "2026-11-13 20:00:03 INFO (MainThread) [homeassistant.core] Home Assistant started\n"
)


class Supervisor(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body="", ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self, method):
        path = self.path.split("?")[0]
        if path == "/_lab/calls":
            with LOCK:
                return self._send(200, list(CALLS))
        if path == "/_lab/clear":
            with LOCK:
                CALLS.clear()
            return self._send(200, {"ok": True})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"result": "error", "message": "unauthorized"})
        with LOCK:
            CALLS.append({"method": method, "path": path})
        if method == "POST" and path == "/core/restart":
            STATE["core_down_until"] = time.time() + RESTART_SECONDS
            return self._send(200, {"result": "ok"})
        if method == "POST" and path.startswith("/addons/") and path.endswith("/restart"):
            return self._send(200, {"result": "ok"})
        if method == "POST" and path == "/host/reboot":
            return self._send(200, {"result": "ok"})
        if method == "GET" and path == "/core/api/":
            if time.time() < STATE["core_down_until"]:
                return self._send(502, "")
            return self._send(200, json.dumps({"message": "API running."}))
        if method == "GET" and path.startswith("/addons/") and path.endswith("/info"):
            return self._send(200, {"result": "ok", "data": {"state": "started", "version": "1.0"}})
        if (
            method == "GET"
            and path in ("/core/logs",)
            or (method == "GET" and path.startswith("/addons/") and path.endswith("/logs"))
        ):
            return self._send(200, LOG, "text/plain")
        self._send(404, {"result": "error", "message": "not found"})

    def do_GET(self):  # noqa: N802
        self.handle_any("GET")

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self.handle_any("POST")


class HomeAssistant(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        data = b'{"message": "API running."}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ha = ThreadingHTTPServer(("0.0.0.0", 8123), HomeAssistant)  # noqa: S104
    threading.Thread(target=ha.serve_forever, daemon=True).start()
    ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "80"))), Supervisor
    ).serve_forever()  # noqa: S104
