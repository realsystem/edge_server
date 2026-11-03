"""telegram-bot/controlplane.py: signed node-to-node messages, replay protection, acks."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("controlplane", REPO / "telegram-bot/controlplane.py")
assert spec and spec.loader
cp = importlib.util.module_from_spec(spec)
sys.modules["controlplane"] = cp
spec.loader.exec_module(cp)


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Net:
    """An in-memory network: addresses map to nodes, and paths can be cut."""

    def __init__(self):
        self.nodes: dict[str, cp.ControlPlane] = {}  # address -> node
        self.down: set[str] = set()  # addresses that do not answer
        self.sent: list[tuple[str, dict]] = []

    def post(self, url: str, data: bytes, timeout: float):
        host = url.split("//")[1].split(":")[0]
        self.sent.append((host, json.loads(data)))
        if host in self.down or host not in self.nodes:
            raise OSError("no route to host")
        status, answer = self.nodes[host].receive(data)
        return status, json.dumps(answer).encode()


def pub(key: ed25519.Ed25519PrivateKey) -> str:
    return cp.public_b64(key)


class Fleet:
    """Three nodes: a and b at one site, c at another."""

    NAMES = ("a", "b", "c")
    SITES = {"a": "s1", "b": "s1", "c": "s2"}

    def __init__(self, tmp_path: Path, clock: Clock):
        self.clock = clock
        self.net = Net()
        self.keys = {n: ed25519.Ed25519PrivateKey.generate() for n in self.NAMES}
        self.tmp = tmp_path
        self.planes: dict[str, cp.ControlPlane] = {}
        for name in self.NAMES:
            self.planes[name] = self.make(name)

    def config(self, name: str, **extra) -> dict:
        peers = []
        for other in self.NAMES:
            if other == name:
                continue
            addrs = {"tailscale": f"ts-{other}"}
            if self.SITES[other] == self.SITES[name]:
                addrs["lan"] = f"lan-{other}"
            peers.append(
                {
                    "name": other,
                    "site": self.SITES[other],
                    "roles": ["base", "nvr"],
                    "public_key": pub(self.keys[other]),
                    "addrs": addrs,
                }
            )
        return {
            "node": name,
            "site": self.SITES[name],
            "roles": ["base", "nvr"],
            "port": 7443,
            "version": "v1.2",
            "fleet_hash": "aaaa1111bbbb",
            "peers": peers,
            **extra,
        }

    def make(self, name: str, config: dict | None = None) -> cp.ControlPlane:
        plane = cp.ControlPlane(
            config or self.config(name),
            self.keys[name],
            self.tmp / f"state-{name}",
            clock=self.clock,
            post=self.net.post,
            uptime=lambda: 3600.0,
            boot_id=f"boot-{name}",
        )
        for address in (f"ts-{name}", f"lan-{name}"):
            self.net.nodes[address] = plane
        return plane


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fleet(tmp_path, clock):
    return Fleet(tmp_path, clock)


def raw(message: dict) -> bytes:
    return json.dumps(message).encode()


def signed(fleet: Fleet, sender: str, to: str, mtype="heartbeat", body=None) -> dict:
    plane = fleet.planes[sender]
    peer = plane.peers[to]
    return plane.message_for(peer, mtype, body if body is not None else plane.heartbeat_body("lan"))


def resign(fleet: Fleet, sender: str, message: dict) -> dict:
    message.pop("sig", None)
    message["sig"] = cp.b64(fleet.keys[sender].sign(cp.canonical(message)))
    return message


class TestAcceptance:
    def test_a_good_heartbeat_is_acked_and_recorded(self, fleet):
        message = signed(fleet, "a", "b")
        status, answer = fleet.planes["b"].receive(raw(message))
        assert status == 200 and answer["type"] == "ack" and answer["ref"] == message["id"]
        assert answer["from"] == "b" and answer["to"] == "a"
        peer = fleet.planes["b"].peers["a"]
        assert peer.version == "v1.2" and peer.fleet_hash == "aaaa1111bbbb"
        assert peer.path_seen["lan"] == fleet.clock.now and peer.boot == "boot-a"

    def test_the_ack_is_signed_by_the_receiver(self, fleet):
        message = signed(fleet, "a", "b")
        _, answer = fleet.planes["b"].receive(raw(message))
        fleet.planes["a"]._check_ack(fleet.planes["a"].peers["b"], answer, message["id"])

    def test_a_message_for_the_whole_fleet_is_accepted(self, fleet):
        message = signed(fleet, "a", "b")
        message["to"] = "fleet"
        assert fleet.planes["b"].receive(raw(resign(fleet, "a", message)))[0] == 200


class TestRejection:
    def reject(self, fleet, message, reason, receiver="b", status=None):
        code, answer = fleet.planes[receiver].receive(raw(message))
        assert answer == {"error": reason}
        assert code == (status or 403)

    def test_tampered_body(self, fleet):
        message = signed(fleet, "a", "b")
        message["body"]["version"] = "v9.9"
        self.reject(fleet, message, "bad signature")

    def test_tampered_envelope(self, fleet):
        message = signed(fleet, "a", "b")
        message["expires"] += 500
        self.reject(fleet, message, "bad signature")

    def test_signed_by_someone_else(self, fleet):
        message = signed(fleet, "a", "b")
        message["from"] = "c"  # claims to be c, signed by a
        self.reject(fleet, message, "bad signature")

    def test_unknown_sender(self, fleet):
        stranger = ed25519.Ed25519PrivateKey.generate()
        message = cp.make_message(
            stranger, "mallory", "b", "heartbeat", {}, now=fleet.clock.now, seq=1, boot="x"
        )
        self.reject(fleet, message, "unknown sender")

    def test_a_forged_key_does_not_help(self, fleet):
        forger = ed25519.Ed25519PrivateKey.generate()
        message = cp.make_message(
            forger, "a", "b", "heartbeat", {}, now=fleet.clock.now, seq=1, boot="x"
        )
        self.reject(fleet, message, "bad signature")

    def test_not_for_this_node(self, fleet):
        self.reject(fleet, signed(fleet, "a", "c"), "not addressed to this node")

    def test_expired(self, fleet):
        message = signed(fleet, "a", "b")
        fleet.clock.now += 10_000
        self.reject(fleet, message, "expired")

    def test_from_the_future(self, fleet):
        message = signed(fleet, "a", "b")
        fleet.clock.now -= cp.FUTURE_SLACK + 60
        self.reject(fleet, message, "from the future (clock difference?)")

    def test_lifetime_too_long(self, fleet):
        message = signed(fleet, "a", "b")
        message["expires"] = message["created"] + cp.MAX_LIFETIME + 1
        self.reject(fleet, resign(fleet, "a", message), "lifetime too long")

    def test_replay_is_refused(self, fleet):
        message = signed(fleet, "a", "b")
        assert fleet.planes["b"].receive(raw(message))[0] == 200
        self.reject(fleet, message, "replayed")

    def test_replay_after_a_restart_is_refused(self, fleet):
        message = signed(fleet, "a", "b")
        assert fleet.planes["b"].receive(raw(message))[0] == 200
        reborn = fleet.make("b")  # same state directory: the seen ids come back from disk
        assert reborn.receive(raw(message)) == (403, {"error": "replayed"})

    def test_a_replay_is_fine_again_only_after_it_expired_and_then_it_is_refused_anyway(
        self, fleet
    ):
        message = signed(fleet, "a", "b")
        assert fleet.planes["b"].receive(raw(message))[0] == 200
        fleet.clock.now += 400
        assert fleet.planes["b"].receive(raw(message)) == (403, {"error": "expired"})

    @pytest.mark.parametrize("junk", [b"", b"not json", b"[]", b'{"v": 1}', b"null"])
    def test_malformed(self, fleet, junk):
        assert fleet.planes["b"].receive(junk) == (400, {"error": "malformed"})

    def test_too_large(self, fleet):
        assert fleet.planes["b"].receive(b"x" * (cp.MAX_BODY + 1)) == (400, {"error": "too large"})

    def test_wrong_protocol_version(self, fleet):
        message = signed(fleet, "a", "b")
        message["v"] = 2
        self.reject(fleet, resign(fleet, "a", message), "unknown protocol version")

    def test_unknown_type_is_not_acted_on(self, fleet):
        message = signed(fleet, "a", "b", mtype="launch-missiles", body={})
        self.reject(fleet, message, "unknown message type")

    def test_a_node_without_a_public_key_is_refused(self, fleet):
        config = fleet.config("b")
        config["peers"][0]["public_key"] = ""
        plane = fleet.make("b", config)
        code, answer = plane.receive(raw(signed(fleet, "a", "b")))
        assert code == 403 and "no public key" in answer["error"]

    def test_a_broken_public_key_in_the_file_is_just_missing(self, fleet, caplog):
        config = fleet.config("b")
        config["peers"][0]["public_key"] = "not-a-key"
        with caplog.at_level(logging.WARNING, logger="edge-bot.control"):
            plane = fleet.make("b", config)
        assert plane.peers["a"].key is None and "public_key is not valid" in caplog.text

    def test_refusals_are_counted_and_logged_once_a_minute(self, fleet, caplog):
        message = signed(fleet, "a", "b")
        message["body"]["version"] = "tampered"
        with caplog.at_level(logging.WARNING, logger="edge-bot.control"):
            for _ in range(50):
                fleet.planes["b"].receive(raw(message))
        assert fleet.planes["b"].peers["a"].rejected == 50
        assert caplog.text.count("rejected a message from a: bad signature") == 1
        fleet.clock.now += 61
        with caplog.at_level(logging.WARNING, logger="edge-bot.control"):
            fleet.planes["b"].receive(raw(message))
        assert caplog.text.count("rejected a message from a: bad signature") == 2

    def test_a_rejected_message_changes_nothing_in_the_view(self, fleet):
        message = signed(fleet, "a", "b")
        message["body"]["version"] = "evil"
        fleet.planes["b"].receive(raw(message))
        assert (
            fleet.planes["b"].peers["a"].version == ""
            and fleet.planes["b"].peers["a"].last_seen == 0
        )

    def test_the_seen_store_is_bounded(self, fleet, monkeypatch):
        monkeypatch.setattr(cp, "MAX_SEEN", 2)
        for _ in range(2):
            assert fleet.planes["b"].receive(raw(signed(fleet, "a", "b")))[0] == 200
        assert fleet.planes["b"].receive(raw(signed(fleet, "a", "b"))) == (
            403,
            {"error": "too many messages in flight"},
        )


class TestAcks:
    def ack_for(self, fleet, message):
        return fleet.planes["b"].receive(raw(message))[1]

    def test_an_ack_for_another_message_is_not_accepted(self, fleet):
        a = fleet.planes["a"]
        first, second = signed(fleet, "a", "b"), signed(fleet, "a", "b")
        ack = self.ack_for(fleet, first)
        with pytest.raises(cp.Reject, match="not the ack"):
            a._check_ack(a.peers["b"], ack, second["id"])

    def test_an_ack_signed_by_a_third_node_is_not_accepted(self, fleet):
        a = fleet.planes["a"]
        message = signed(fleet, "a", "b")
        forged = cp.make_message(
            fleet.keys["c"],
            "b",
            "a",
            "ack",
            {},
            now=fleet.clock.now,
            seq=1,
            boot="x",
            ref=message["id"],
        )
        with pytest.raises(cp.Reject, match="bad signature"):
            a._check_ack(a.peers["b"], forged, message["id"])

    def test_an_ack_that_claims_another_sender_is_not_accepted(self, fleet):
        a = fleet.planes["a"]
        message = signed(fleet, "a", "b")
        ack = fleet.planes["c"].receive(raw(signed(fleet, "a", "c")))[1]
        ack["ref"] = message["id"]
        with pytest.raises(cp.Reject):
            a._check_ack(a.peers["b"], ack, message["id"])


class TestSending:
    def test_heartbeats_reach_every_peer_and_are_recorded_on_both_sides(self, fleet):
        fleet.planes["a"].heartbeat_tick()
        a, b, c = (fleet.planes[n] for n in "abc")
        assert b.peers["a"].last_seen and c.peers["a"].last_seen  # they heard a
        assert a.peers["b"].last_ack and a.peers["c"].last_ack  # a heard their acks

    def test_tailscale_is_tried_first_and_the_lan_is_the_fallback(self, fleet):
        fleet.net.down.add("ts-b")
        fleet.planes["a"].heartbeat_tick()
        a, b = fleet.planes["a"], fleet.planes["b"]
        assert b.peers["a"].path_seen == {"lan": fleet.clock.now}
        assert "tailscale" in a.peers["b"].path_failed and "lan" in a.peers["b"].path_seen

    def test_probing_checks_every_path_and_tells_them_apart(self, fleet):
        fleet.net.down.add("lan-b")
        fleet.planes["a"].heartbeat_tick(probe_all_paths=True)
        peer = fleet.planes["a"].peers["b"]
        assert "tailscale" in peer.path_seen and "lan" in peer.path_failed
        text = fleet.planes["a"].fleet_text()
        assert "tailscale ok" in text and "lan failing" in text

    def test_a_peer_at_another_site_is_only_tried_on_tailscale(self, fleet):
        fleet.planes["a"].heartbeat_tick(probe_all_paths=True)
        hosts = {host for host, msg in fleet.net.sent if msg["to"] == "c"}
        assert hosts == {"ts-c"}

    def test_partition_between_sites_and_recovery(self, fleet):
        fleet.net.down |= {"ts-c"}
        fleet.planes["a"].heartbeat_tick()
        assert fleet.planes["a"].peers["c"].last_ack == 0
        assert "failing" in fleet.planes["a"].fleet_text()
        fleet.net.down.clear()
        fleet.clock.now += 60
        fleet.planes["a"].heartbeat_tick()
        assert fleet.planes["a"].peers["c"].last_ack == fleet.clock.now

    def test_a_peer_that_answers_garbage_is_not_alive(self, fleet):
        a = fleet.planes["a"]

        def liar(url, data, timeout):
            return 200, b'{"type": "ack", "ref": "x"}'

        a.post = liar
        a.heartbeat_tick()
        assert a.peers["b"].last_ack == 0

    def test_a_peer_that_answers_an_error_is_not_alive(self, fleet):
        a = fleet.planes["a"]
        a.post = lambda url, data, timeout: (403, b'{"error": "bad signature"}')
        a.heartbeat_tick()
        assert a.peers["b"].last_ack == 0

    def test_every_message_has_its_own_id_and_a_rising_sequence(self, fleet):
        a = fleet.planes["a"]
        a.heartbeat_tick()
        a.heartbeat_tick()
        mine = [m for _, m in fleet.net.sent if m["from"] == "a"]
        assert len({m["id"] for m in mine}) == len(mine)
        seqs = [m["seq"] for m in mine]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    def test_the_sequence_survives_a_restart_and_never_goes_back(self, fleet):
        a = fleet.planes["a"]
        a.heartbeat_tick()
        last = a.counter.value
        reborn = fleet.make("a")
        assert reborn.counter.next() == last + 1

    def test_without_saved_state_the_sequence_starts_from_the_clock(self, tmp_path):
        counter = cp.Counter(tmp_path / "none", 1_800_000_000.7)
        assert counter.next() == 1_800_000_001


class TestReliable:
    def test_kept_until_the_peer_answers(self, fleet):
        a = fleet.planes["a"]
        fleet.net.down |= {"ts-c"}
        a.send_reliable("c", "heartbeat", a.heartbeat_body("tailscale"))
        assert len(a.outbox.pending(fleet.clock.now)) == 1
        fleet.net.down.clear()
        a.flush_outbox()
        assert a.outbox.pending(fleet.clock.now) == []
        assert fleet.planes["c"].peers["a"].last_seen

    def test_survives_a_restart(self, fleet):
        a = fleet.planes["a"]
        fleet.net.down |= {"ts-c"}
        a.send_reliable("c", "heartbeat", a.heartbeat_body("tailscale"))
        reborn = fleet.make("a")
        assert len(reborn.outbox.pending(fleet.clock.now)) == 1
        fleet.net.down.clear()
        reborn.flush_outbox()
        assert reborn.outbox.pending(fleet.clock.now) == []

    def test_dropped_when_it_expires(self, fleet):
        a = fleet.planes["a"]
        fleet.net.down |= {"ts-c"}
        a.send_reliable("c", "heartbeat", {}, ttl=120)
        fleet.clock.now += 121
        assert a.outbox.pending(fleet.clock.now) == []

    def test_delivering_twice_does_no_harm(self, fleet):
        a = fleet.planes["a"]
        message = a.message_for(a.peers["c"], "heartbeat", a.heartbeat_body("tailscale"))
        assert fleet.planes["c"].receive(raw(message))[0] == 200
        assert fleet.planes["c"].receive(raw(message))[0] == 403  # the retry is a replay: refused


class TestReport:
    def test_never_heard_from(self, fleet):
        text = fleet.planes["a"].fleet_text()
        assert "b: never heard from or answered" in text and "c: never heard from" in text
        assert "this node: edge_server v1.2, fleet file aaaa1111" in text

    def test_after_contact(self, fleet):
        for name in "abc":
            fleet.planes[name].heartbeat_tick()
        fleet.clock.now += 30
        text = fleet.planes["a"].fleet_text()
        assert "b: last contact 30 s ago" in text and "edge_server v1.2" in text
        assert "(same)" in text and "DIFFERENT" not in text

    def test_a_different_fleet_file_and_version_are_called_out(self, fleet):
        other = fleet.make("b", fleet.config("b", fleet_hash="ffff0000", version="v1.3"))
        other.heartbeat_tick()
        text = fleet.planes["a"].fleet_text()
        assert "(DIFFERENT)" in text and "software version differs" in text

    def test_a_clock_that_is_off_is_reported_but_accepted(self, fleet):
        message = signed(fleet, "a", "b")
        fleet.clock.now -= 90  # b's clock is behind a's by 90 s: still inside the slack
        assert fleet.planes["b"].receive(raw(message))[0] == 200
        assert "clock +90 s off" in fleet.planes["b"].fleet_text()

    def test_a_missing_key_is_said_plainly(self, fleet):
        config = fleet.config("b")
        config["peers"][0]["public_key"] = ""
        text = fleet.make("b", config).fleet_text()
        assert "a: no public_key in the fleet file" in text

    def test_refusals_show_up(self, fleet):
        message = signed(fleet, "a", "b")
        message["body"]["x"] = 1
        fleet.planes["b"].receive(raw(message))
        assert "1 message(s) refused, last: bad signature" in fleet.planes["b"].fleet_text()

    def test_ages(self):
        assert [cp.human_age(s) for s in (5, 120, 7200, 400_000)] == ["5 s", "2 min", "2 h", "4 d"]


class TestKeys:
    def test_load_and_round_trip(self, tmp_path):
        from cryptography.hazmat.primitives import serialization

        key = ed25519.Ed25519PrivateKey.generate()
        path = tmp_path / "node.key"
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        loaded = cp.load_private_key(path)
        assert cp.public_b64(loaded) == cp.public_b64(key)
        cp.load_public_key(cp.public_b64(key)).verify(loaded.sign(b"x"), b"x")

    def test_not_an_ed25519_key(self, tmp_path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        path = tmp_path / "other.key"
        path.write_bytes(
            ec.generate_private_key(ec.SECP256R1()).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        with pytest.raises(ValueError, match="not an Ed25519 key"):
            cp.load_private_key(path)

    @pytest.mark.parametrize("text", ["", "abc", "AAAA", "!!!!"])
    def test_bad_public_keys(self, text):
        with pytest.raises(ValueError):
            cp.load_public_key(text)


class TestOverHttp:
    """The same thing over real sockets on the loopback interface."""

    @pytest.fixture
    def pair(self, tmp_path, clock):
        keys = {n: ed25519.Ed25519PrivateKey.generate() for n in "ab"}
        planes = {}
        for name, other in (("a", "b"), ("b", "a")):
            config = {
                "node": name,
                "site": "s",
                "roles": ["base"],
                "port": 0,
                "version": "v1",
                "fleet_hash": "h",
                "peers": [
                    {"name": other, "public_key": pub(keys[other]), "addrs": {"lan": "127.0.0.1:1"}}
                ],
            }
            planes[name] = cp.ControlPlane(config, keys[name], tmp_path / name, uptime=lambda: 1.0)
        ports = {n: planes[n].serve("127.0.0.1", 0) for n in "ab"}
        planes["a"].peers["b"].addrs["lan"] = f"127.0.0.1:{ports['b']}"
        planes["b"].peers["a"].addrs["lan"] = f"127.0.0.1:{ports['a']}"
        yield planes, ports
        for plane in planes.values():
            plane.shutdown()

    def test_heartbeats_both_ways(self, pair):
        planes, _ = pair
        planes["a"].heartbeat_tick()
        planes["b"].heartbeat_tick()
        assert planes["a"].peers["b"].last_ack and planes["a"].peers["b"].last_seen
        assert planes["b"].peers["a"].last_ack and planes["b"].peers["a"].last_seen

    def post(self, port, data, path="/v1/message", headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_a_forged_message_is_refused_over_the_wire(self, pair):
        planes, ports = pair
        forger = ed25519.Ed25519PrivateKey.generate()
        message = cp.make_message(forger, "a", "b", "heartbeat", {}, now=1e9, seq=1, boot="x")
        status, body = self.post(ports["b"], raw(message))
        assert status == 403 and json.loads(body) == {"error": "bad signature"}

    def test_a_replay_is_refused_over_the_wire(self, pair):
        planes, ports = pair
        a = planes["a"]
        message = a.message_for(a.peers["b"], "heartbeat", a.heartbeat_body("lan"))
        assert self.post(ports["b"], raw(message))[0] == 200
        status, body = self.post(ports["b"], raw(message))
        assert status == 403 and json.loads(body) == {"error": "replayed"}

    def test_other_paths_and_methods_do_nothing(self, pair):
        _, ports = pair
        assert self.post(ports["b"], b"x", path="/")[0] == 404
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{ports['b']}/v1/message", timeout=5)
        assert caught.value.code in (404, 501)

    def test_an_oversized_body_is_not_read(self, pair):
        _, ports = pair
        status, _ = self.post(ports["b"], b"x" * (cp.MAX_BODY + 10))
        assert status == 413

    def test_a_dead_peer_does_not_hang_or_crash(self, pair):
        planes, _ = pair
        planes["b"].shutdown()
        planes["a"].heartbeat_tick()
        assert planes["a"].peers["b"].last_ack == 0 and "lan" in planes["a"].peers["b"].path_failed
