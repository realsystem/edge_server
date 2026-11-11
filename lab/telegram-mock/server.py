#!/usr/bin/env python3
"""telegram-mock: a stand-in for api.telegram.org, for lab runs without a real bot (CI).

The bot talks to it through TELEGRAM_API_URL: getUpdates (long poll), sendMessage,
setMyCommands, getMe, sendDocument. Admin:
  POST /_lab/say   {"text": "/status", "user_id": 1001}   a message from a user to the bot
  GET  /_lab/sent  -> {"messages": [{"chat_id":..,"text":..}, ...]}   what the bot said
  POST /_lab/clear
Standard library only.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Condition()
UPDATES = []   # [{"update_id", "message"}]
SENT = []
NEXT_ID = [1]
COMMANDS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/_lab/sent":
            with LOCK:
                return self._send({"messages": list(SENT)})
        if self.path.endswith("/getMe"):
            return self._send({"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "lab", "username": "lab_mock_bot"}})
        self._send({"ok": False, "description": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        ctype = self.headers.get("Content-Type", "")
        try:
            body = json.loads(raw) if "json" in ctype or raw[:1] == b"{" else {}
        except ValueError:
            body = {}
        path = self.path
        if path == "/_lab/say":
            with LOCK:
                uid = int(body.get("user_id", 1001))
                UPDATES.append({"update_id": NEXT_ID[0], "message": {
                    "message_id": NEXT_ID[0], "date": int(time.time()), "text": body["text"],
                    "from": {"id": uid, "is_bot": False, "first_name": "lab"},
                    "chat": {"id": uid, "type": "private"}}})
                NEXT_ID[0] += 1
                LOCK.notify_all()
            return self._send({"ok": True})
        if path == "/_lab/clear":
            with LOCK:
                SENT.clear()
            return self._send({"ok": True})
        method = path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            offset = int(body.get("offset") or 0)
            deadline = time.time() + min(int(body.get("timeout") or 0), 25)
            with LOCK:
                while True:
                    fresh = [u for u in UPDATES if u["update_id"] >= offset]
                    if fresh or time.time() >= deadline:
                        return self._send({"ok": True, "result": fresh})
                    LOCK.wait(timeout=max(0.1, deadline - time.time()))
        if method == "sendMessage":
            with LOCK:
                SENT.append({"chat_id": body.get("chat_id"), "text": body.get("text", ""), "at": time.time()})
            return self._send({"ok": True, "result": {"message_id": len(SENT)}})
        if method == "sendDocument":
            with LOCK:
                SENT.append({"chat_id": None, "text": "[document]", "at": time.time()})
            return self._send({"ok": True, "result": {"message_id": len(SENT)}})
        if method == "setMyCommands":
            COMMANDS[:] = body.get("commands", [])
            return self._send({"ok": True, "result": True})
        self._send({"ok": True, "result": True})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8081"))), Handler).serve_forever()
