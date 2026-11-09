"""The control plane on autonomy_core: Autonomy envelopes, negotiation and fallback.

Uses the Fleet, Net and Clock helpers of test_controlplane.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
core = pytest.importorskip("autonomy_core")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_controlplane import Clock, Fleet, cp, raw  # noqa: E402


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fleet(tmp_path, clock):
    return Fleet(tmp_path, clock)


def envelopes(net, host=None):
    return [m for h, m in net.sent if core.is_envelope(m) and (host is None or h == host)]


def legacy(net, host=None):
    return [m for h, m in net.sent if not core.is_envelope(m) and (host is None or h == host)]


def converge(fleet, rounds=2):
    for _ in range(rounds):
        for plane in fleet.planes.values():
            plane.heartbeat_tick()


def envelope_from(fleet, sender, to, **kw):
    plane = fleet.planes[sender]
    args = {
        "source": f"cell:{sender}",
        "type": "health.heartbeat",
        "data": {**plane.heartbeat_body("lan"), "to": to, "boot": plane.boot},
        "seq": plane.counter.next(),
        "now": fleet.clock.now,
        "ttl": 180,
    }
    args.update(kw)
    return core.make_envelope(fleet.keys[sender], **args)


class OldNode:
    """Stands in for a node running the previous release: it refuses envelopes the way
    that release does (HTTP 400 malformed) and never advertises them."""

    def __init__(self, fleet, name):
        self.plane = fleet.planes[name]
        self.plane.use_envelope = False
        original = self.plane.receive

        def receive(data: bytes):
            if core.is_envelope(json.loads(data)):
                return 400, {"error": "malformed"}
            return original(data)

        self.plane.receive = receive


class TestNegotiation:
    def test_first_contact_is_legacy_then_envelopes(self, fleet):
        fleet.planes["a"].heartbeat_tick()
        assert envelopes(fleet.net) == [] and legacy(fleet.net)
        converge(fleet)
        fleet.net.sent.clear()
        fleet.planes["a"].heartbeat_tick()
        sent = envelopes(fleet.net)
        assert sent and not legacy(fleet.net)
        env = sent[0]
        assert env["source"] == "cell:a" and env["type"] == "health.heartbeat"
        assert env["data"]["to"] in ("b", "c") and env["data"]["envelope"] == cp.ENVELOPE
        assert fleet.planes["b"].peers["a"].last_seen == fleet.clock.now

    def test_heartbeat_contents_are_kept(self, fleet, clock):
        converge(fleet)
        clock.now += 30
        fleet.planes["a"].heartbeat_tick()
        peer = fleet.planes["b"].peers["a"]
        assert peer.version == "v1.2" and peer.fleet_hash == "aaaa1111bbbb"
        assert peer.boot == "boot-a" and peer.uptime == 3600.0 and peer.roles == ["base", "nvr"]
        assert peer.skew == 0 and 0 < peer.seq <= fleet.planes["a"].counter.value

    def test_acks_are_envelopes_bound_to_the_message(self, fleet):
        converge(fleet)
        b = fleet.planes["b"]
        env = envelope_from(fleet, "a", "b")
        status, ack = b.receive(raw(env))
        assert status == 200 and core.is_envelope(ack)
        assert ack["type"] == "control.ack" and ack["causationid"] == env["id"]
        assert ack["correlationid"] == env["correlationid"] and ack["data"] == {"to": "a"}
        assert ack["traceparent"].split("-")[1] == env["traceparent"].split("-")[1]
        fleet.planes["a"]._check_ack(fleet.planes["a"].peers["b"], ack, env["id"])
        with pytest.raises(cp.Reject, match="not the ack"):
            fleet.planes["a"]._check_ack(fleet.planes["a"].peers["b"], ack, "other-id")

    def test_disabled_in_fleet_file(self, fleet, tmp_path):
        fleet.planes["b"] = fleet.make("b", fleet.config("b", envelope=False))
        converge(fleet, 3)
        assert envelopes(fleet.net, "lan-b") == [] and envelopes(fleet.net, "ts-b") == []
        assert "envelope" not in fleet.planes["b"].heartbeat_body("lan")
        # it still accepts them
        assert fleet.planes["b"].receive(raw(envelope_from(fleet, "a", "b")))[0] == 200

    def test_old_node_in_the_fleet(self, fleet):
        OldNode(fleet, "c")
        converge(fleet, 3)
        assert envelopes(fleet.net, "ts-c") == []
        assert fleet.planes["a"].peers["c"].last_ack and fleet.planes["c"].peers["a"].last_seen
        assert envelopes(fleet.net, "ts-b")  # a and b still talk envelopes

    def test_a_peer_that_refuses_envelopes_gets_legacy(self, fleet, clock):
        converge(fleet)
        a = fleet.planes["a"]
        assert a.speaks_envelope(a.peers["c"])
        OldNode(fleet, "c")  # c is downgraded but a still believes it takes envelopes
        clock.now += 60
        a.heartbeat_tick()  # this round is refused with 400
        assert not a.peers["c"].envelope
        clock.now += 60
        fleet.net.sent.clear()
        a.heartbeat_tick()
        assert legacy(fleet.net, "ts-c") and fleet.planes["c"].peers["a"].last_seen == clock.now

    def test_a_peer_that_stops_advertising_gets_legacy(self, fleet):
        converge(fleet)
        fleet.planes["b"].use_envelope = False
        fleet.planes["b"].heartbeat_tick()
        assert not fleet.planes["a"].speaks_envelope(fleet.planes["a"].peers["b"])

    def test_names_the_contracts_cannot_carry_stay_legacy(self, tmp_path, clock):
        class Odd(Fleet):
            NAMES = ("a--1", "b")
            SITES = {"a--1": "s1", "b": "s1"}

        odd = Odd(tmp_path, clock)
        converge(odd, 3)
        assert envelopes(odd.net) == []
        assert odd.planes["b"].peers["a--1"].last_seen

    def test_fleet_text_shows_the_protocol(self, fleet):
        converge(fleet)
        assert "envelopes" in fleet.planes["a"].fleet_text()
        OldNode(fleet, "c")
        fleet.planes["c"].heartbeat_tick()
        assert "legacy messages" in fleet.planes["a"].fleet_text()


class TestEnvelopeChecks:
    @pytest.mark.parametrize(
        "change,error",
        [
            (lambda e: e["data"].update(uptime=1), "bad signature"),
            (lambda e: e.update(seq=1), "bad signature"),
            (lambda e: e.pop("autonomysig"), "malformed"),
            (lambda e: e.update(specversion="0.3"), "unknown protocol version"),
        ],
    )
    def test_tampered(self, fleet, change, error):
        env = envelope_from(fleet, "a", "b")
        change(env)
        status, answer = fleet.planes["b"].receive(raw(env))
        assert answer == {"error": error} and status == (400 if error == "malformed" else 403)
        assert fleet.planes["b"].peers["a"].rejected == 1

    def test_replay_survives_restart(self, fleet):
        env = envelope_from(fleet, "a", "b")
        assert fleet.planes["b"].receive(raw(env))[0] == 200
        assert fleet.planes["b"].receive(raw(env)) == (403, {"error": "replayed"})
        reborn = fleet.make("b")
        assert reborn.receive(raw(env)) == (403, {"error": "replayed"})

    def test_unknown_and_unaddressed(self, fleet):
        stranger = core.generate_private_key()
        env = core.make_envelope(
            stranger,
            source="cell:z",
            type="health.heartbeat",
            data={"to": "b"},
            seq=1,
            now=fleet.clock.now,
            ttl=60,
        )
        assert fleet.planes["b"].receive(raw(env)) == (403, {"error": "unknown sender"})
        forged = core.make_envelope(
            stranger,
            source="cell:a",
            type="health.heartbeat",
            data={"to": "b"},
            seq=1,
            now=fleet.clock.now,
            ttl=60,
        )
        assert fleet.planes["b"].receive(raw(forged)) == (403, {"error": "bad signature"})
        wrong = envelope_from(fleet, "a", "c")
        assert fleet.planes["b"].receive(raw(wrong)) == (
            403,
            {"error": "not addressed to this node"},
        )
        fleet_wide = envelope_from(fleet, "a", "fleet")
        assert fleet.planes["b"].receive(raw(fleet_wide))[0] == 200
        other = envelope_from(fleet, "a", "b", type="control.reboot")
        assert fleet.planes["b"].receive(raw(other)) == (403, {"error": "unknown message type"})

    @pytest.mark.parametrize(
        "kw,offset,error",
        [
            ({"ttl": 60}, 61, "expired"),
            ({"ttl": cp.MAX_LIFETIME + 1}, 0, "lifetime too long"),
            ({"ttl": None}, 0, "malformed"),  # commands between nodes must expire
            ({"ttl": 60, "now": 1_800_000_000.0 + cp.FUTURE_SLACK + 1}, 0, "from the future"),
        ],
    )
    def test_freshness(self, fleet, clock, kw, offset, error):
        env = envelope_from(fleet, "a", "b", **kw)
        clock.now += offset
        assert fleet.planes["b"].receive(raw(env))[1] == {"error": error}

    def test_a_node_without_key_is_refused(self, fleet):
        config = fleet.config("b")
        config["peers"][0]["public_key"] = ""
        b = fleet.make("b", config)
        env = envelope_from(fleet, "a", "b")
        status, answer = b.receive(raw(env))
        assert status == 403 and "no public key" in answer["error"]


class TestReliableEnvelopes:
    def test_outbox_keeps_envelopes_until_acked(self, fleet, clock):
        converge(fleet)
        a = fleet.planes["a"]
        fleet.net.down |= {"ts-c"}
        a.send_reliable("c", "heartbeat", a.heartbeat_body("tailscale"))
        pending = a.outbox.pending(clock.now)
        assert len(pending) == 1 and core.is_envelope(pending[0])
        reborn = fleet.make("a")
        reborn.peers["c"].envelope = True
        fleet.net.down.clear()
        reborn.flush_outbox()
        assert reborn.outbox.pending(clock.now) == []
        assert fleet.planes["c"].peers["a"].last_seen

    def test_lifetime_is_capped(self, fleet, clock):
        converge(fleet)
        a = fleet.planes["a"]
        env = a.message_for(a.peers["b"], "heartbeat", a.heartbeat_body("lan"), ttl=3600)
        sent, expires = core.envelope_times(env)
        assert expires - sent == cp.MAX_LIFETIME


def test_keys_in_either_encoding(fleet):
    key = fleet.keys["a"]
    assert cp.load_public_key(core.encode_public_key(key)) is not None
    assert cp.load_public_key(cp.public_b64(key)) is not None
    with pytest.raises(ValueError, match="not a base64 Ed25519 public key"):
        cp.load_public_key("nope")


def test_types_round_trip():
    for mtype in ("heartbeat", "ack", "reboot_request"):
        assert cp.legacy_type(cp.envelope_type(mtype)) == mtype


def test_library_pins_agree():
    """pyproject.toml, requirements.txt and the bot image install the same autonomy-core."""
    import re

    repo = Path(__file__).resolve().parent.parent
    pins = {
        name: re.findall(
            r"realsystem/autonomy/archive/([0-9a-f]{40})\.tar\.gz", (repo / name).read_text()
        )
        for name in ("pyproject.toml", "requirements.txt")
    }
    docker = re.findall(
        r"^ARG AUTONOMY_CORE_REF=([0-9a-f]{40})$",
        (repo / "telegram-bot/Dockerfile").read_text(),
        re.M,
    )
    refs = {ref for found in [*pins.values(), docker] for ref in found}
    assert len(refs) == 1 and all(pins.values()) and docker, (pins, docker)
