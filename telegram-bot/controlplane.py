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

Autonomy envelopes. The same messages can travel as Autonomy envelopes (CloudEvents with
an Ed25519 signature over RFC 8785 canonical JSON; spec v0 in realsystem/autonomy). The
primitives - envelope build and verify, seen ids, sequence counter, outbox - come from
the ``autonomy_core`` library. A node accepts both forms, advertises
``"envelope": "0.1"`` in its heartbeat, and sends envelopes only to peers that advertise
it too; a peer that answers HTTP 400 to an envelope, or stops advertising, gets the
original form again. ``"envelope": false`` in fleet.json turns envelopes off. Node ``x``
signs as ``cell:x``; the heartbeat is ``health.heartbeat``, the ack ``control.ack`` (its
``causationid`` is the acked id), and the addressee goes in ``data.to``.

Signed registry. When the fleet file is an Autonomy registry, fleet.json carries it and
its owner signature; the node verifies them (``autonomy_core.RegistryStore``) against the
root key it pinned on first use, refuses older serials, and keeps the last valid registry
when an update is refused. Peer keys then come only from the verified registry, and the
components it lists for this node may send ``health.reported`` / ``health.heartbeat``
envelopes, shown under /fleet. Unknown and revoked identities are refused.

Needs the ``cryptography`` and ``autonomy-core`` packages (the bot image installs them
for fleets).
"""

from __future__ import annotations

import base64
import json
import logging
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

import autonomy_core as core
from autonomy_core import Reject

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
ENVELOPE = "0.1"  # spec version advertised in heartbeats
ENVELOPE_TYPES = {"heartbeat": "health.heartbeat", "ack": "control.ack"}  # others: control.<type>


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
    """A node's public key as written in fleet.yaml: base64 of the 32 raw bytes (the
    ``ed25519:<base64url>`` form is accepted too)."""
    try:
        return core.decode_public_key(text)
    except ValueError as exc:
        raise ValueError("not a base64 Ed25519 public key") from exc


def envelope_type(mtype: str) -> str:
    return ENVELOPE_TYPES.get(mtype, f"control.{mtype}")


def legacy_type(etype: str) -> str:
    for mtype, name in ENVELOPE_TYPES.items():
        if name == etype:
            return mtype
    return etype[len("control.") :] if etype.startswith("control.") else etype


def recipient(message: dict) -> Any:
    """Who a message is for, in either form."""
    if core.is_envelope(message):
        data = message.get("data")
        return data.get("to") if isinstance(data, dict) else None
    return message.get("to")


class SeenStore(core.SeenStore):
    """Message ids already accepted, kept until they expire, on disk (autonomy_core).
    The capacity follows MAX_SEEN."""

    def __init__(self, path: Path):
        super().__init__(path, MAX_SEEN)

    @property
    def max_entries(self) -> int:
        return MAX_SEEN

    @max_entries.setter
    def max_entries(self, value: int) -> None:
        pass  # always MAX_SEEN


Counter = core.Counter  # a number that only goes up and survives restarts (seq)
Outbox = core.Outbox  # messages kept on disk and retried until acked or expired


@dataclass
class ComponentState:
    """A component the registry lists for this node, and what it last reported."""

    id: str
    version: str
    last_seen: float = 0.0
    status: str = ""
    reported_version: str = ""
    failing: list[str] = field(default_factory=list)
    rejected: int = 0
    last_reject: str = ""


COMPONENT_TYPES = ("health.reported", "health.heartbeat")
COMPONENT_FRESHNESS = core.Freshness(max_lifetime=MAX_LIFETIME, future_slack=FUTURE_SLACK)


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
    envelope: bool = False  # it advertised Autonomy envelopes in its last heartbeat


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
        # envelopes: node x signs as cell:x; names the contracts cannot carry stay legacy
        self.source = f"cell:{self.me}"
        self.use_envelope = config.get("envelope", True) is not False and core.is_name(self.me)
        self.registry: Any = None  # core.Registry when the fleet file is a signed registry
        self.registry_problem = ""
        self.components: dict[str, ComponentState] = {}
        self.revoked_ids: set[str] = set()
        if config.get("registry") is not None:
            self._load_registry(config, state_dir / "registry")
        self.keyring = core.KeyRing()
        for peer in self.peers.values():
            if peer.key is not None and core.is_name(peer.name):
                self.keyring.add(f"cell:{peer.name}", peer.key)
        if self.registry is not None:
            for comp in self.registry.components_for(self.me):
                self.keyring.add(comp.id, comp.key)
        self.freshness = core.Freshness(
            max_lifetime=MAX_LIFETIME, future_slack=FUTURE_SLACK, require_expires=True
        )
        self.servers: list[ThreadingHTTPServer] = []
        self.stop = threading.Event()
        self.last_path_check = 0.0
        self.handled: dict[str, Callable[[Peer, dict], None]] = {
            "heartbeat": self._on_heartbeat,
            "command_request": self._on_command_request,
        }
        # set by the bot (peer commands, ADR-018); raises Reject to refuse a request
        self.on_command: Callable[[Peer, dict], None] | None = None
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
            if core.is_envelope(message):
                return self._receive_envelope(message, now)
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
            return self._refuse(claimed, why, now)
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

    def _load_registry(self, config: dict, store_dir: Path) -> None:
        """Verify the registry from fleet.json; peer keys and components come from it."""
        store = core.RegistryStore(store_dir)
        offered, signature = config["registry"], config.get("registry_signature") or {}
        try:
            reg = store.accept(offered, signature, now=self.clock())
        except Reject as why:
            held = store.current(now=self.clock())
            if held is None:
                raise ValueError(f"the registry was refused: {why}") from None
            reg = held
            self.registry_problem = f"registry update refused ({why}); using serial {held.serial}"
            log.warning("control: %s", self.registry_problem)
        self.registry = reg
        for peer in self.peers.values():
            key = reg.cell_keys.get(peer.name)
            if key is None and peer.key is not None:
                log.warning("control: %s is not in the verified registry; refusing it", peer.name)
            peer.key = key
        for comp in reg.components_for(self.me):
            self.components[comp.id] = ComponentState(comp.id, comp.version)
        self.revoked_ids = {str(r.get("id", "")).split("@")[0] for r in reg.doc.get("revoked", [])}
        log.info(
            "control: registry serial %d (%s), %d component(s) for this node",
            reg.serial,
            reg.signer,
            len(self.components),
        )

    def _receive_component(self, env: dict, source: str, now: float) -> tuple[int, dict]:
        base, _, cell = source.partition("@")
        state = self.components.get(base)
        try:
            if state is None:
                raise Reject("revoked" if base in self.revoked_ids else "unknown sender")
            if cell and cell != self.me:
                raise Reject("not addressed to this node")
            core.verify_envelope(
                env, self.keyring, now=now, freshness=COMPONENT_FRESHNESS, seen=self.seen
            )
            if env["type"] not in COMPONENT_TYPES:
                raise Reject("unknown message type")
            data = env.get("data")
            with self.lock:
                state.last_seen = now
                if env["type"] == "health.reported" and isinstance(data, dict):
                    state.status = str(data.get("status", ""))
                    state.reported_version = str(data.get("version", ""))
                    state.failing = [
                        str(c.get("name"))
                        for c in data.get("checks", [])
                        if isinstance(c, dict) and c.get("ok") is False
                    ]
        except Reject as why:
            if state is not None:
                with self.lock:
                    state.rejected += 1
                    state.last_reject = why.reason
            log.warning("control: refused a message from %s: %s", source[:64], why.reason)
            status = 400 if why.reason in ("malformed", "too large") else 403
            return status, {"error": why.reason}
        ack = core.make_envelope(
            self.key,
            source=self.source,
            type=envelope_type("ack"),
            data={"to": source},
            seq=self.counter.next(),
            now=now,
            ttl=60,
            correlationid=str(env["correlationid"]),
            causationid=str(env["id"]),
            traceparent=core.child_traceparent(str(env["traceparent"])),
        )
        return 200, ack

    def _refuse(self, claimed: Any, why: Reject, now: float) -> tuple[int, dict]:
        self._rejected(claimed, why.reason, now)
        status = 400 if why.reason in ("malformed", "too large") else 403
        return status, {"error": why.reason}

    def _envelope_peer(self, env: dict) -> Peer:
        """The fleet node an envelope claims to come from (not yet verified)."""
        source = env.get("source")
        name = (
            source[len("cell:") :]
            if isinstance(source, str) and source.startswith("cell:")
            else None
        )
        peer = self.peers.get(name) if name else None
        if peer is None:
            raise Reject("unknown sender")
        if peer.key is None:
            raise Reject("no public key for the sender in the fleet file")
        if f"cell:{peer.name}" not in self.keyring:
            raise Reject("unknown sender")
        return peer

    def _receive_envelope(self, env: dict, now: float) -> tuple[int, dict]:
        source = env.get("source")
        if (
            isinstance(source, str)
            and not source.startswith("cell:")
            and core.is_component_ref(source)
        ):
            return self._receive_component(env, source, now)
        claimed: Any = source[len("cell:") :] if isinstance(source, str) else None
        try:
            peer = self._envelope_peer(env)
            core.verify_envelope(
                env, self.keyring, now=now, freshness=self.freshness, seen=self.seen
            )
            data = env.get("data")
            if not isinstance(data, dict):
                raise Reject("malformed")
            if data.get("to") not in (self.me, "fleet"):
                raise Reject("not addressed to this node")
            mtype = legacy_type(str(env["type"]))
            handler = self.handled.get(mtype)
            if handler is None:
                raise Reject("unknown message type")
            with self.lock:
                handler(peer, self._as_message(env, peer, mtype))
        except Reject as why:
            return self._refuse(claimed, why, now)
        ack = core.make_envelope(
            self.key,
            source=self.source,
            type=envelope_type("ack"),
            data={"to": peer.name},
            seq=self.counter.next(),
            now=now,
            ttl=60,
            correlationid=str(env["correlationid"]),
            causationid=str(env["id"]),
            traceparent=core.child_traceparent(str(env["traceparent"])),
        )
        return 200, ack

    @staticmethod
    def _as_message(env: dict, peer: Peer, mtype: str) -> dict:
        """An accepted envelope in the original message form, for the handlers."""
        sent, expires = core.envelope_times(env)
        body = {k: v for k, v in env["data"].items() if k not in ("to", "boot")}
        message = {
            "v": PROTOCOL,
            "id": env["id"],
            "from": peer.name,
            "to": env["data"]["to"],
            "created": sent,
            "expires": expires,
            "seq": env["seq"],
            "boot": str(env["data"].get("boot", "")),
            "type": mtype,
            "body": body,
        }
        if "causationid" in env:
            message["ref"] = env["causationid"]
        return message

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

    def _on_command_request(self, peer: Peer, message: dict) -> None:
        """A request to restart a service or reboot (a signed ``peer-request`` in ``request``).
        Receiving is not doing: the bot's guard checks signature, grant and the owner's code."""
        request = message["body"].get("request")
        if not isinstance(request, dict):
            raise Reject("malformed")
        if self.on_command is None:
            raise Reject("peer commands are off")
        self.on_command(peer, request)

    def send_command(self, node: str, request: dict, ttl: float = 120.0) -> str | None:
        """Send a signed ``peer-request`` to ``node`` once (no retries: its code expires). Returns the
        path that answered with a valid ack, or None."""
        peer = self.peers.get(node)
        if peer is None:
            return None
        return self.send(peer, self.message_for(peer, "command_request", {"request": request}, ttl))

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
        peer.envelope = str(body.get("envelope", "")).split(".")[0] == ENVELOPE.split(".")[0]

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
                if status == 400 and core.is_envelope(message) and peer.envelope:
                    peer.envelope = False  # an older node: back to the original form
                    log.info(
                        "control: %s does not take envelopes; using legacy messages", peer.name
                    )
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
        if core.is_envelope(ack):
            if self._envelope_peer(ack) is not peer:
                raise Reject("ack from another node")
            core.verify_envelope(ack, self.keyring, now=self.clock(), freshness=self.freshness)
            data = ack.get("data")
            if (
                ack["type"] != envelope_type("ack")
                or ack.get("causationid") != sent_id
                or not isinstance(data, dict)
                or data.get("to") != self.me
            ):
                raise Reject("not the ack for this message")
            return
        if not isinstance(ack, dict) or any(k not in ack for k in REQUIRED):
            raise Reject("malformed ack")
        if ack["type"] != "ack" or ack.get("ref") != sent_id or ack["to"] != self.me:
            raise Reject("not the ack for this message")
        if check_signature(ack, self.peers) is not peer:
            raise Reject("ack from another node")
        check_fresh(ack, self.clock())

    def speaks_envelope(self, peer: Peer) -> bool:
        return self.use_envelope and peer.envelope and f"cell:{peer.name}" in self.keyring

    def message_for(self, peer: Peer, mtype: str, body: dict, ttl: float = 300.0) -> dict:
        if self.speaks_envelope(peer):
            return core.make_envelope(
                self.key,
                source=self.source,
                type=envelope_type(mtype),
                data={**body, "to": peer.name, "boot": self.boot},
                seq=self.counter.next(),
                now=self.clock(),
                ttl=min(ttl, MAX_LIFETIME),
            )
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
        } | ({"envelope": ENVELOPE} if self.use_envelope else {})

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
            to = recipient(message)
            peer = self.peers.get(to) if isinstance(to, str) else None
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
        if self.registry is not None:
            reg = self.registry
            lines.append(
                f"  registry serial {reg.serial}, signed by {reg.signer}, "
                f"root {core.fingerprint(reg.root)[:19]}"
            )
        if self.registry_problem:
            lines.append(f"  WARNING: {self.registry_problem}")
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
            if peer.last_seen and self.use_envelope:
                details.append("envelopes" if self.speaks_envelope(peer) else "legacy messages")
            if details:
                lines.append("  " + ", ".join(details))
            if peer.rejected:
                lines.append(f"  {peer.rejected} message(s) refused, last: {peer.last_reject}")
        if self.components:
            lines.append("")
            lines.append(f"Components ({len(self.components)}):")
            for comp in sorted(self.components.values(), key=lambda c: c.id):
                if not comp.last_seen:
                    text = f"  {comp.id} {comp.version}: never reported"
                else:
                    text = f"  {comp.id} {comp.reported_version or comp.version}: {comp.status or 'alive'}, {human_age(now - comp.last_seen)} ago"
                    if comp.failing:
                        text += f", failing: {', '.join(comp.failing)}"
                if comp.rejected:
                    text += f"; {comp.rejected} refused, last: {comp.last_reject}"
                lines.append(text)
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
