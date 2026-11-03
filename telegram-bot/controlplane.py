"""Signed messages between the nodes of a fleet (the control plane).

Used by bot.py when the deploy script wrote a fleet description for a fleet of two or
more nodes (see docs/design/edge-system.md, "Telegram: operator group, bots and
protocol"). Nothing here talks to Telegram: nodes reach each other over Tailscale and
the site LAN, and every message is signed with the sending node's own Ed25519 key.

A message is one JSON object::

    {"v": 1, "id": ..., "from": node, "to": node or "fleet", "created": epoch,
     "expires": epoch, "seq": n, "boot": boot id, "type": ..., "ref": optional,
     "body": {...}, "sig": base64 of the signature over everything else}

A node accepts a message only if it is addressed to it, comes from a node in the fleet
file, carries a valid signature, is fresh (not expired, not from the future, not living
longer than ten minutes) and has an ``id`` it has not seen. Seen ids are kept on disk, so
a restart does not reopen replays. The receiver answers with a signed ``ack``; the sender
treats a valid ack as proof that the peer is alive on that path. A message that matters
can go through the persistent ``Outbox``: it is retried until acked or expired.

A message never runs anything: the heartbeat only updates what ``/fleet`` shows.
Needs the ``cryptography`` package (the bot image installs it for fleets).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger("edge-bot.control")

PROTOCOL = 1
MAX_BODY = 64 * 1024  # bytes; heartbeats are a few hundred
MAX_LIFETIME = 600  # seconds a message may live
FUTURE_SLACK = 120  # a message created further ahead than this of our clock is refused
CLOCK_WARN = 30  # seconds of clock difference worth a warning
HEARTBEAT_INTERVAL = 60.0
PATH_CHECK_INTERVAL = 300.0  # every path of every peer is probed this often
PATHS = ("tailscale", "lan")
MAX_SEEN = 10000
SEND_TIMEOUT = 5.0


class Reject(Exception):
    """A message that must not be acted on; the text is the reason (logged, sent back)."""


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def canonical(message: dict) -> bytes:
    """The bytes that are signed: the message without its signature, in a fixed form."""
    plain = {k: v for k, v in message.items() if k != "sig"}
    return json.dumps(plain, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _crypto() -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return ed25519, serialization


def load_private_key(path: str | Path) -> Any:
    ed25519, serialization = _crypto()
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 key")
    return key


def public_b64(private_key: Any) -> str:
    _, serialization = _crypto()
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return b64(raw)


def load_public_key(text: str) -> Any:
    """A node's public key as written in fleet.yaml: base64 of the 32 raw bytes."""
    ed25519, _ = _crypto()
    try:
        raw = base64.b64decode(text.strip(), validate=True)
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not a base64 Ed25519 public key") from exc


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class SeenStore:
    """Message ids already accepted, kept until they expire, on disk."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.ids: dict[str, float] = {}
        try:
            data = json.loads(path.read_text())
            self.ids = {str(k): float(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError):
            if path.exists():
                log.warning("control: %s is unreadable; starting with no seen ids", path)

    def add(self, message_id: str, expires: float, now: float) -> bool:
        """True if new (and now remembered), False if it was seen before."""
        with self.lock:
            self.ids = {i: e for i, e in self.ids.items() if e > now}
            if message_id in self.ids:
                return False
            if len(self.ids) >= MAX_SEEN:
                raise Reject("too many messages in flight")
            self.ids[message_id] = expires
            try:
                _atomic_write(self.path, json.dumps(self.ids))
            except OSError as exc:
                log.warning("control: cannot save seen ids: %s", exc)
            return True


class Counter:
    """A number that only goes up and survives restarts (message sequence numbers)."""

    def __init__(self, path: Path, start: float):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.value = int(path.read_text().strip())
        except (OSError, ValueError):
            self.value = int(start)  # no state: the clock keeps it ahead of anything sent before

    def next(self) -> int:
        with self.lock:
            self.value += 1
            try:
                _atomic_write(self.path, str(self.value))
            except OSError as exc:
                log.warning("control: cannot save the sequence number: %s", exc)
            return self.value


class Outbox:
    """Messages that must arrive: kept on disk and retried until acked or expired."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.items: list[dict] = []
        try:
            self.items = list(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            self.items = []

    def _save(self) -> None:
        try:
            _atomic_write(self.path, json.dumps(self.items))
        except OSError as exc:
            log.warning("control: cannot save the outbox: %s", exc)

    def add(self, message: dict) -> None:
        with self.lock:
            self.items.append(message)
            self._save()

    def pending(self, now: float) -> list[dict]:
        with self.lock:
            kept = [m for m in self.items if m.get("expires", 0) > now]
            if len(kept) != len(self.items):
                self.items = kept
                self._save()
            return list(kept)

    def done(self, message_id: str) -> None:
        with self.lock:
            self.items = [m for m in self.items if m.get("id") != message_id]
            self._save()


@dataclass
class Peer:
    name: str
    site: str = ""
    roles: list[str] = field(default_factory=list)
    key: Any = None  # public key; None = fleet.yaml has no public_key for it yet
    addrs: dict[str, str] = field(default_factory=dict)  # path -> "host" or "host:port"
    # what we have seen of it
    last_seen: float = 0.0  # a valid message from it
    last_ack: float = 0.0  # a valid ack from it (it answered us)
    path_seen: dict[str, float] = field(default_factory=dict)  # path -> last valid contact
    path_failed: dict[str, float] = field(default_factory=dict)  # path -> last failed attempt
    version: str = ""
    fleet_hash: str = ""
    boot: str = ""
    uptime: float = 0.0
    seq: int = 0
    skew: float = 0.0
    rejected: int = 0
    last_reject: str = ""


def make_message(
    private_key: Any,
    me: str,
    to: str,
    mtype: str,
    body: dict,
    *,
    now: float,
    seq: int,
    boot: str,
    ttl: float = 300.0,
    ref: str | None = None,
) -> dict:
    message: dict[str, Any] = {
        "v": PROTOCOL,
        "id": uuid.uuid4().hex,
        "from": me,
        "to": to,
        "created": int(now),
        "expires": int(now + min(ttl, MAX_LIFETIME)),
        "seq": seq,
        "boot": boot,
        "type": mtype,
        "body": body,
    }
    if ref:
        message["ref"] = ref
    message["sig"] = b64(private_key.sign(canonical(message)))
    return message


def check_signature(message: dict, peers: dict[str, Peer]) -> Peer:
    """The sender's entry if the message is signed by it; otherwise raise Reject."""
    sender = message.get("from")
    peer = peers.get(sender) if isinstance(sender, str) else None
    if peer is None:
        raise Reject("unknown sender")
    if peer.key is None:
        raise Reject("no public key for the sender in the fleet file")
    try:
        peer.key.verify(
            base64.b64decode(str(message.get("sig", "")), validate=True), canonical(message)
        )
    except Exception:
        raise Reject("bad signature") from None
    return peer


def check_fresh(message: dict, now: float) -> None:
    created, expires = message.get("created"), message.get("expires")
    if not isinstance(created, int | float) or not isinstance(expires, int | float):
        raise Reject("malformed")
    if expires <= now:
        raise Reject("expired")
    if created > now + FUTURE_SLACK:
        raise Reject("from the future (clock difference?)")
    if expires - created > MAX_LIFETIME:
        raise Reject("lifetime too long")


REQUIRED = ("v", "id", "from", "to", "created", "expires", "seq", "type", "body", "sig")


def http_post(url: str, data: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_BODY)


def human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


class ControlPlane:
    """This node's side of the control plane: sign, send, receive, verify, remember."""

    def __init__(
        self,
        config: dict,
        private_key: Any,
        state_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        post: Callable[[str, bytes, float], tuple[int, bytes]] = http_post,
        uptime: Callable[[], float] = lambda: 0.0,
        boot_id: str = "",
    ):
        self.me: str = config["node"]
        self.site: str = config.get("site", "")
        self.roles: list[str] = list(config.get("roles", []))
        self.port: int = int(config.get("port", 7443))
        self.version: str = config.get("version", "")
        self.fleet_hash: str = config.get("fleet_hash", "")
        self.key = private_key
        self.clock = clock
        self.post = post
        self.uptime = uptime
        self.boot = boot_id or uuid.uuid4().hex
        self.heartbeat_interval = float(config.get("heartbeat_interval", HEARTBEAT_INTERVAL))
        self.path_check_interval = float(config.get("path_check_interval", PATH_CHECK_INTERVAL))
        state_dir.mkdir(parents=True, exist_ok=True)
        self.seen = SeenStore(state_dir / "seen.json")
        self.counter = Counter(state_dir / "seq", clock())
        self.outbox = Outbox(state_dir / "outbox.json")
        self.started = clock()
        self.lock = threading.Lock()
        self.peers: dict[str, Peer] = {}
        for entry in config.get("peers", []):
            key = None
            if entry.get("public_key"):
                try:
                    key = load_public_key(entry["public_key"])
                except ValueError:
                    log.warning("control: %s: public_key is not valid", entry["name"])
            self.peers[entry["name"]] = Peer(
                name=entry["name"],
                site=entry.get("site", ""),
                roles=list(entry.get("roles", [])),
                key=key,
                addrs={p: a for p, a in entry.get("addrs", {}).items() if p in PATHS and a},
            )
        self.servers: list[ThreadingHTTPServer] = []
        self.stop = threading.Event()
        self.last_path_check = 0.0
        self.handled: dict[str, Callable[[Peer, dict], None]] = {"heartbeat": self._on_heartbeat}
        self._log_times: dict[tuple, float] = {}

    # ------------------------------------------------------------ receiving
    def receive(self, raw: bytes) -> tuple[int, dict]:
        """Handle one posted message. Returns the HTTP status and the JSON answer."""
        now = self.clock()
        peer: Peer | None = None
        claimed: Any = None
        try:
            if len(raw) > MAX_BODY:
                raise Reject("too large")
            try:
                message = json.loads(raw)
            except ValueError:
                raise Reject("malformed") from None
            if not isinstance(message, dict) or any(k not in message for k in REQUIRED):
                raise Reject("malformed")
            if message["v"] != PROTOCOL:
                raise Reject("unknown protocol version")
            claimed = message["from"]
            if message["to"] not in (self.me, "fleet"):
                raise Reject("not addressed to this node")
            if not isinstance(message["id"], str) or not isinstance(message["body"], dict):
                raise Reject("malformed")
            peer = check_signature(message, self.peers)
            check_fresh(message, now)
            if not self.seen.add(message["id"], float(message["expires"]), now):
                raise Reject("replayed")
            handler = self.handled.get(str(message["type"]))
            if handler is None:
                raise Reject("unknown message type")
            with self.lock:
                handler(peer, message)
        except Reject as why:
            self._rejected(claimed, str(why), now)
            return (400 if str(why) in ("malformed", "too large") else 403), {"error": str(why)}
        ack = make_message(
            self.key,
            self.me,
            peer.name,
            "ack",
            {},
            now=now,
            seq=self.counter.next(),
            boot=self.boot,
            ttl=60,
            ref=message["id"],
        )
        return 200, ack

    def _rejected(self, claimed: Any, reason: str, now: float) -> None:
        """Count and log a refusal. The sender is whoever the message *claims* to be from:
        the counter and the log tell the operator where to look, nothing trusts them."""
        known = self.peers.get(claimed) if isinstance(claimed, str) else None
        who = str(claimed)[:40] if isinstance(claimed, str) else "(unknown)"
        if known:
            known.rejected += 1
            known.last_reject = reason
        # one log line per sender and reason a minute: a flood must not fill the disk
        key = (who, reason)
        if now - self._log_times.get(key, 0.0) >= 60:
            self._log_times[key] = now
            log.warning("control: rejected a message from %s: %s", who, reason)

    def _on_heartbeat(self, peer: Peer, message: dict) -> None:
        now = self.clock()
        body = message["body"]
        via = body.get("via") if body.get("via") in PATHS else "?"
        peer.last_seen = now
        peer.path_seen[via] = now
        peer.version = str(body.get("version", ""))
        peer.fleet_hash = str(body.get("fleet_hash", ""))
        peer.boot = str(message.get("boot", ""))
        peer.uptime = float(body.get("uptime", 0) or 0)
        peer.roles = [str(r) for r in body.get("roles", peer.roles)]
        peer.skew = float(message["created"]) - now
        peer.seq = int(message["seq"])

    # -------------------------------------------------------------- sending
    def _url(self, addr: str) -> str:
        host, _, port = addr.partition(":") if addr.count(":") == 1 else (addr, "", "")
        return f"http://{host}:{port or self.port}/v1/message"

    def send(self, peer: Peer, message: dict, paths: tuple[str, ...] = PATHS) -> str | None:
        """Post ``message`` over the first path that answers; return that path or None.

        An answer counts only if it is a valid, signed ack for this very message.
        """
        data = json.dumps(message).encode()
        for path in paths:
            addr = peer.addrs.get(path)
            if not addr:
                continue
            now = self.clock()
            try:
                status, answer = self.post(self._url(addr), data, SEND_TIMEOUT)
                if status != 200:
                    raise Reject(f"answered HTTP {status}")
                ack = json.loads(answer)
                self._check_ack(peer, ack, message["id"])
            except (OSError, ValueError, Reject, urllib.error.URLError) as exc:
                peer.path_failed[path] = now
                log.debug("control: %s over %s: %s", peer.name, path, exc)
                continue
            with self.lock:
                peer.last_ack = now
                peer.path_seen[path] = now
                peer.path_failed.pop(path, None)
            return path
        return None

    def _check_ack(self, peer: Peer, ack: Any, sent_id: str) -> None:
        if not isinstance(ack, dict) or any(k not in ack for k in REQUIRED):
            raise Reject("malformed ack")
        if ack["type"] != "ack" or ack.get("ref") != sent_id or ack["to"] != self.me:
            raise Reject("not the ack for this message")
        if check_signature(ack, self.peers) is not peer:
            raise Reject("ack from another node")
        check_fresh(ack, self.clock())

    def message_for(self, peer: Peer, mtype: str, body: dict, ttl: float = 300.0) -> dict:
        return make_message(
            self.key,
            self.me,
            peer.name,
            mtype,
            body,
            now=self.clock(),
            seq=self.counter.next(),
            boot=self.boot,
            ttl=ttl,
        )

    def heartbeat_body(self, via: str) -> dict:
        return {
            "roles": self.roles,
            "uptime": round(self.uptime(), 1),
            "version": self.version,
            "fleet_hash": self.fleet_hash,
            "via": via,
        }

    def heartbeat_tick(self, probe_all_paths: bool = False) -> None:
        """One heartbeat to every peer, over the first path that works (or over each
        path in turn when ``probe_all_paths``, to learn which paths are up)."""
        for peer in list(self.peers.values()):
            if probe_all_paths:
                for path in PATHS:
                    if path in peer.addrs:
                        self._heartbeat(peer, (path,))
            else:
                self._heartbeat(peer, PATHS)
        self.flush_outbox()

    def _heartbeat(self, peer: Peer, paths: tuple[str, ...]) -> None:
        for path in paths:
            if path not in peer.addrs:
                continue
            message = self.message_for(
                peer, "heartbeat", self.heartbeat_body(path), ttl=self.heartbeat_interval * 3
            )
            if self.send(peer, message, (path,)):
                return

    def send_reliable(self, peer_name: str, mtype: str, body: dict, ttl: float = 3600.0) -> str:
        """Queue a message that must arrive (kept on disk, retried until acked)."""
        peer = self.peers[peer_name]
        message = self.message_for(peer, mtype, body, ttl=ttl)
        self.outbox.add(message)
        self.flush_outbox()
        return str(message["id"])

    def flush_outbox(self) -> None:
        for message in self.outbox.pending(self.clock()):
            peer = self.peers.get(message["to"])
            if peer and self.send(peer, message):
                self.outbox.done(message["id"])

    # ---------------------------------------------------------------- loops
    def run_loop(self) -> None:
        while not self.stop.is_set():
            now = self.clock()
            probe = now - self.last_path_check >= self.path_check_interval
            if probe:
                self.last_path_check = now
            try:
                self.heartbeat_tick(probe_all_paths=probe)
            except Exception:
                log.exception("control: heartbeat round failed")
            self.stop.wait(self.heartbeat_interval)

    def serve(self, bind: str = "0.0.0.0", port: int | None = None) -> int:  # noqa: S104
        """Start listening (in a thread). Returns the port."""
        plane = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 5

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if self.path != "/v1/message" or length > MAX_BODY:
                    self.send_response(404 if self.path != "/v1/message" else 413)
                    self.end_headers()
                    return
                status, answer = plane.receive(self.rfile.read(length))
                payload = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer((bind, self.port if port is None else port), Handler)
        server.daemon_threads = True
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return int(server.server_address[1])

    def start(self, bind: str = "0.0.0.0") -> None:  # noqa: S104
        port = self.serve(bind)
        log.info(
            "control plane: node %s listening on port %d, %d peer(s)",
            self.me,
            port,
            len(self.peers),
        )
        for peer in self.peers.values():
            if peer.key is None:
                log.warning(
                    "control: no public_key for %s in the fleet file: its messages are refused",
                    peer.name,
                )
        threading.Thread(target=self.run_loop, daemon=True).start()

    def shutdown(self) -> None:
        self.stop.set()
        for server in self.servers:
            server.shutdown()
            server.server_close()

    # --------------------------------------------------------------- report
    def fleet_text(self) -> str:
        now = self.clock()
        lines = [
            f"Fleet: {self.me} (site {self.site or '-'}), {len(self.peers)} other node(s)",
            f"  this node: edge_server {self.version or '?'}, fleet file {self.fleet_hash[:8] or '?'}",
        ]
        for peer in sorted(self.peers.values(), key=lambda p: p.name):
            lines.append("")
            if peer.key is None:
                lines.append(
                    f"{peer.name}: no public_key in the fleet file, so its messages are refused"
                )
                continue
            if not peer.last_seen and not peer.last_ack:
                lines.append(
                    f"{peer.name}: never heard from or answered ({self._paths(peer, now)})"
                )
            else:
                last = max(peer.last_seen, peer.last_ack)
                lines.append(
                    f"{peer.name}: last contact {human_age(now - last)} ago ({self._paths(peer, now)})"
                )
            details = []
            if peer.version:
                details.append(f"edge_server {peer.version}")
            if peer.fleet_hash:
                same = peer.fleet_hash == self.fleet_hash
                details.append(
                    f"fleet file {peer.fleet_hash[:8]} {'(same)' if same else '(DIFFERENT)'}"
                )
            if peer.version and self.version and peer.version != self.version:
                details.append("software version differs")
            if peer.last_seen and abs(peer.skew) > CLOCK_WARN:
                details.append(f"clock {peer.skew:+.0f} s off")
            if peer.last_seen:
                details.append(f"up {human_age(peer.uptime)}")
            if details:
                lines.append("  " + ", ".join(details))
            if peer.rejected:
                lines.append(f"  {peer.rejected} message(s) refused, last: {peer.last_reject}")
        return "\n".join(lines)

    @staticmethod
    def _paths(peer: Peer, now: float) -> str:
        parts = []
        for path in PATHS:
            if path not in peer.addrs:
                continue
            seen = peer.path_seen.get(path, 0.0)
            failed = peer.path_failed.get(path, 0.0)
            if seen and seen >= failed:
                parts.append(f"{path} ok {human_age(now - seen)} ago")
            elif failed:
                parts.append(f"{path} failing since {human_age(now - failed)} ago")
            else:
                parts.append(f"{path} not tried")
        return ", ".join(parts) or "no address"


def load_config(path: str | Path) -> dict:
    config: dict = json.loads(Path(path).read_text())
    return config
