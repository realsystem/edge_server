"""Signed Autonomy registry as the fleet file (Autonomy milestone M3).

ST-01: a component developed independently - here a mock built only on autonomy_core -
is refused by a node until the owner adds it to the registry and signs one update; the
node then accepts it with no code change, and refuses it again once it is revoked.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
core = pytest.importorskip("autonomy_core")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_controlplane import Clock, Fleet, cp, pub, raw  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import fleet as fleet_tool  # noqa: E402

OPS_EXPIRES = "2030-01-01T00:00:00Z"


class Owner:
    """The owner's keys and registry, as `autonomy registry` would keep them."""

    def __init__(self, keys: dict, sites: dict, clock: Clock):
        self.root = core.generate_private_key()
        self.ops = core.generate_private_key()
        self.clock = clock
        self.serial = 0
        self.doc = {
            "registry_format": 1,
            "schema_version": 1,
            "fleet": {"telegram": {"operators": [1]}, "alert_order": sorted(keys)},
            "sites": dict.fromkeys(sorted(set(sites.values()))),
            "owners": core.sign_owners(
                {"ops": [{"key": core.encode_public_key(self.ops), "expires": OPS_EXPIRES}]},
                self.root,
            ),
            "nodes": {
                n: {"site": sites[n], "packaging": "host", "roles": ["nvr"], "public_key": pub(k)}
                for n, k in sorted(keys.items())
            },
            "components": {},
            "revoked": [],
        }

    def sign(self, doc: dict | None = None, serial: int | None = None) -> tuple[dict, dict]:
        doc = copy.deepcopy(self.doc if doc is None else doc)
        self.serial = serial if serial is not None else self.serial + 1
        return doc, core.sign_registry(doc, self.ops, serial=self.serial, now=self.clock.now)

    def enrol(self, component: MockComponent, cells: list[str]) -> None:
        self.doc["components"][component.id] = {
            "public_key": core.encode_public_key(component.key),
            "version": "1.0.0",
            "manifest_hash": core.sha256_tagged(core.canonicalize(component.manifest)),
            "cells": cells,
            "contracts": component.manifest["contracts"],
        }

    def revoke(self, cid: str) -> None:
        entry = self.doc["components"].pop(cid)
        self.doc["revoked"].append(
            {
                "id": cid,
                "key": entry["public_key"],
                "revoked_at": "2027-01-15T08:00:00Z",
                "reason": "test",
            }
        )


class MockComponent:
    """A third-party component: its own key and manifest, autonomy_core only."""

    def __init__(self, cid: str = "svc:mock-sensor"):
        self.id = cid
        self.key = core.generate_private_key()
        self.counter = core.Counter(None, start=0)
        self.manifest = {
            "component_id": cid,
            "version": "1.0.0",
            "kind": "svc",
            "description": "mock sensor",
            "contracts": {"envelope": "^0.1", "health": "^0.1"},
            "capabilities": {},
            "trust_class": "trusted-input",
            "runtime": {"packaging": ["binary"]},
        }

    def health(self, now: float, source: str | None = None, **data) -> dict:
        body = {"status": "ok", "component_id": self.id, "version": "1.0.0", "uptime_s": 60}
        body.update(data)
        return core.make_envelope(
            self.key,
            source=source or self.id,
            type="health.reported",
            dataschema="https://autonomy.local/spec/v0/health.schema.json",
            data=body,
            seq=self.counter.next(),
            now=now,
            ttl=120,
        )


class RegistryFleet(Fleet):
    """Fleet whose nodes get their keys from a signed registry."""

    def config(self, name: str, **extra) -> dict:
        if not hasattr(self, "owner"):
            self.owner = Owner(self.keys, self.SITES, self.clock)
            self.offered = self.owner.sign()
        doc, sig = extra.pop("registry", self.offered)
        return super().config(name, registry=doc, registry_signature=sig, **extra)

    def redeploy(self, name: str, offered: tuple[dict, dict]) -> cp.ControlPlane:
        """Same code, new fleet.json: what deploy-edge-server.sh does with a new registry."""
        self.planes[name] = self.make(name, self.config(name, registry=offered))
        return self.planes[name]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fleet(tmp_path, clock):
    return RegistryFleet(tmp_path, clock)


def converge(fleet, rounds=2):
    for _ in range(rounds):
        for plane in fleet.planes.values():
            plane.heartbeat_tick()


class TestST01:
    def test_new_component_accepted_after_one_signed_update(self, fleet, clock):
        mock = MockComponent()
        b = fleet.planes["b"]
        assert b.receive(raw(mock.health(clock.now))) == (403, {"error": "unknown sender"})

        fleet.owner.enrol(mock, cells=["b"])
        b = fleet.redeploy("b", fleet.owner.sign())  # one signed update, no code change

        status, ack = b.receive(
            raw(mock.health(clock.now, checks=[{"name": "probe", "ok": False, "impact": "x"}]))
        )
        assert status == 200 and ack["type"] == "control.ack" and ack["data"] == {"to": mock.id}
        # the component checks the ack with the registry, like any other envelope
        reg = core.verify_registry(
            *fleet.offered_for_check(b),
            root=core.encode_public_key(fleet.owner.root),
            now=clock.now,
        )
        assert core.verify_envelope(ack, reg.keyring(), now=clock.now) == "cell:b"
        text = b.fleet_text()
        assert (
            "Components (1):" in text
            and "svc:mock-sensor 1.0.0: ok" in text
            and "failing: probe" in text
        )
        assert "registry serial 2" in text

        # a node the registry does not list it for refuses it
        a = fleet.redeploy("a", fleet.owner.sign(serial=fleet.owner.serial))
        assert a.receive(raw(mock.health(clock.now))) == (403, {"error": "unknown sender"})

        fleet.owner.revoke(mock.id)
        b = fleet.redeploy("b", fleet.owner.sign())
        assert b.receive(raw(mock.health(clock.now))) == (403, {"error": "revoked"})
        assert "Components" not in b.fleet_text()

    def test_instances_and_message_checks(self, fleet, clock):
        mock = MockComponent()
        fleet.owner.enrol(mock, cells=["b"])
        b = fleet.redeploy("b", fleet.owner.sign())
        assert b.receive(raw(mock.health(clock.now, source=f"{mock.id}@b")))[0] == 200
        assert b.receive(raw(mock.health(clock.now, source=f"{mock.id}@a"))) == (
            403,
            {"error": "not addressed to this node"},
        )
        env = mock.health(clock.now)
        assert b.receive(raw(env))[0] == 200
        assert b.receive(raw(env)) == (403, {"error": "replayed"})
        tampered = mock.health(clock.now)
        tampered["data"]["status"] = "down"
        assert b.receive(raw(tampered)) == (403, {"error": "bad signature"})
        other = core.make_envelope(
            mock.key, source=mock.id, type="job.queued", data={}, seq=99, now=clock.now, ttl=60
        )
        assert b.receive(raw(other)) == (403, {"error": "unknown message type"})
        impostor = core.make_envelope(
            core.generate_private_key(),
            source=mock.id,
            type="health.reported",
            data={},
            seq=1,
            now=clock.now,
            ttl=60,
        )
        assert b.receive(raw(impostor)) == (403, {"error": "bad signature"})
        assert "5 refused, last: bad signature" in b.fleet_text()


class TestRegistryOnNodes:
    def test_nodes_talk_with_registry_keys(self, fleet):
        converge(fleet)
        a = fleet.planes["a"]
        assert all(p.last_seen for p in a.peers.values())
        assert "registry serial 1, signed by owner:ops" in a.fleet_text()

    def test_older_registry_refused_last_valid_kept(self, fleet, clock):
        first = fleet.offered
        mock = MockComponent()
        fleet.owner.enrol(mock, cells=["a"])
        fleet.redeploy("a", fleet.owner.sign())
        a = fleet.redeploy("a", first)  # someone deploys the old file
        assert a.registry.serial == 2 and "rollback" in a.registry_problem
        assert "WARNING: registry update refused" in a.fleet_text()
        assert a.receive(raw(mock.health(clock.now)))[0] == 200  # still the serial-2 registry

    def test_foreign_root_is_refused(self, fleet, tmp_path, clock):
        forged = copy.deepcopy(fleet.owner.doc)
        other_root, other_ops = core.generate_private_key(), core.generate_private_key()
        forged["owners"] = core.sign_owners(
            {"ops": [{"key": core.encode_public_key(other_ops), "expires": OPS_EXPIRES}]},
            other_root,
        )
        forged_sig = core.sign_registry(forged, other_ops, serial=50, now=clock.now)
        a = fleet.redeploy("a", (forged, forged_sig))
        assert a.registry.serial == 1 and "untrusted root" in a.registry_problem
        # a brand-new node pins the first root it sees, so a forged first registry is a
        # deploy-time decision; without any valid registry the control plane stays off
        with pytest.raises(ValueError, match="registry was refused"):
            cp.ControlPlane(
                fleet.config("c", registry=(forged, {**forged_sig, "serial": 0})),
                fleet.keys["c"],
                tmp_path / "fresh",
                clock=clock,
            )

    def test_peer_missing_from_registry_is_refused(self, fleet, clock):
        doc = copy.deepcopy(fleet.owner.doc)
        del doc["nodes"]["c"]
        doc["fleet"]["alert_order"].remove("c")
        a = fleet.redeploy("a", fleet.owner.sign(doc))
        assert a.peers["c"].key is None
        fleet.planes["c"].heartbeat_tick()
        assert a.peers["c"].last_seen == 0
        assert "c: no public_key" in a.fleet_text()


def _offered_for_check(self, plane):
    return plane.registry.doc, plane.registry.signature


RegistryFleet.offered_for_check = _offered_for_check  # type: ignore[attr-defined]


# --- tools/fleet.py with a registry --------------------------------------------------------


def write_registry(tmp_path: Path) -> tuple[Path, Owner]:
    data, _ = fleet_tool.parse_file(str(REPO / "examples/fleet.two-site.yaml"))
    keys = {n: core.generate_private_key() for n in data["nodes"]}
    sites = {n: node["site"] for n, node in data["nodes"].items()}
    import time

    real = Clock()
    real.now = time.time()
    owner = Owner(keys, sites, real)
    doc = copy.deepcopy(data)
    for n in doc["nodes"]:
        doc["nodes"][n]["public_key"] = pub(keys[n])
    doc.update({k: owner.doc[k] for k in ("registry_format", "owners", "components", "revoked")})
    owner.doc = doc
    doc, sig = owner.sign()
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc, indent=2))
    (tmp_path / "registry.sig.json").write_text(json.dumps(sig))
    return path, owner


class TestFleetTool:
    def test_check_verifies_and_bot_config_embeds(self, tmp_path, capsys):
        path, owner = write_registry(tmp_path)
        assert fleet_tool.main(["check", str(path), "--node", "remote-nvr"]) == 0
        out = capsys.readouterr().out
        assert "registry serial 1 signed by owner:ops" in out and "signature OK" in out
        cfg_path = tmp_path / "fleet.json"
        assert (
            fleet_tool.main(
                ["bot-config", str(path), "--node", "remote-nvr", "--out", str(cfg_path)]
            )
            == 0
        )
        cfg = json.loads(cfg_path.read_text())
        assert cfg["registry"] == json.loads(path.read_text())
        assert cfg["registry_signature"]["serial"] == 1
        assert {p["name"] for p in cfg["peers"]} == {"remote-ha", "home-server", "home-ha"}

    def test_missing_or_bad_signature(self, tmp_path, capsys):
        path, _ = write_registry(tmp_path)
        doc = json.loads(path.read_text())
        doc["nodes"]["home-ha"]["tailscale"] = "somewhere-else"
        path.write_text(json.dumps(doc))
        assert fleet_tool.main(["check", str(path)]) == 1
        assert "registry hash mismatch" in capsys.readouterr().err + capsys.readouterr().out
        (tmp_path / "registry.sig.json").unlink()
        assert fleet_tool.main(["env", str(path), "--node", "remote-nvr"]) == 1
        assert (
            fleet_tool.main(
                ["bot-config", str(path), "--node", "remote-nvr", "--out", str(tmp_path / "x")]
            )
            == 1
        )

    def test_registry_must_be_the_signed_json(self, tmp_path, capsys):
        path, _ = write_registry(tmp_path)
        import yaml

        as_yaml = tmp_path / "registry.yaml"
        as_yaml.write_text(yaml.safe_dump(json.loads(path.read_text())))
        (tmp_path / "registry.yaml.sig.json").write_text("{}")
        assert fleet_tool.main(["check", str(as_yaml)]) == 1
        assert "must be the JSON file" in capsys.readouterr().err

    def test_every_registry_node_needs_a_key(self, tmp_path):
        path, _ = write_registry(tmp_path)
        doc = json.loads(path.read_text())
        del doc["nodes"]["home-ha"]["public_key"]
        with pytest.raises(fleet_tool.FleetError) as exc:
            fleet_tool.parse(json.dumps(doc))
        assert any("every node of a registry needs one" in m for _, m in exc.value.errors)

    def test_both_key_encodings(self):
        key = core.generate_private_key()
        assert fleet_tool.public_key_ok(pub(key)) and fleet_tool.public_key_ok(
            core.encode_public_key(key)
        )
        assert not fleet_tool.public_key_ok("ed25519:short") and not fleet_tool.public_key_ok(5)

    def test_signature_path(self):
        assert fleet_tool.signature_path("/x/registry.json") == Path("/x/registry.sig.json")
        assert fleet_tool.signature_path("/x/fleet.yaml") == Path(
            "/x/fleet.sig.json"
        )  # the saved copy

    def test_deploy_keeps_the_signature_with_the_saved_file(self):
        text = (REPO / "deploy-edge-server.sh").read_text()
        assert (
            'sig_src="${FLEET_FILE%.*}.sig.json" sig_dst="${FLEET_SAVED_FILE%.*}.sig.json"' in text
        )
