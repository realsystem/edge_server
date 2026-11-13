# ruff: noqa: F811, F401, E402 - pytest fixtures are imported from the other test modules and used as arguments
"""A one-node install can hold an owner-signed policy bundle: its registry comes from the state
directory (state/registry), as the fleet's does, so the bundle's grants are verified."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
core = pytest.importorskip("autonomy_core")
if not hasattr(core, "RegistryStore"):
    pytest.skip("autonomy-core without the registry", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_governance import COMPOSE, audit_kinds
from test_telegram_bot import ALICE, bot, frigate, host, make_bot, reboot, send, telegram

from autonomy_core.ids import to_rfc3339
from autonomy_core.registry import sign_owners, sign_registry


def authority(state: Path, now: float, *, with_registry: bool = True, tamper: bool = False) -> str:
    """What ops/lab/authority/make_authority.py writes: registry store plus a signed bundle."""
    root, ops = core.generate_private_key(), core.generate_private_key()
    owners = sign_owners(
        {"ops": [{"key": core.encode_public_key(ops), "expires": to_rfc3339(now + 86400)}]}, root
    )
    registry = {
        "registry_format": 1,
        "schema_version": 1,
        "owners": owners,
        "nodes": {},
        "components": {},
        "revoked": [],
    }
    if with_registry:
        store = state / "registry"
        store.mkdir()
        (store / "registry.json").write_text(json.dumps(registry))
        (store / "registry.sig.json").write_text(
            json.dumps(sign_registry(registry, ops, serial=1, signer="owner:ops", now=now))
        )
        (store / "owner-root.pub").write_text(core.encode_public_key(root) + "\n")
    grant = {
        "grant_id": "grt_t",
        "grantee": "agent:node-assistant",
        "responsibility": "node.assistance",
        "autonomy_level": "A1",
        "actions": ["host.restart_service"],
        "resources": {"services": ["frigate"]},
        "constraints": {"max_risk_class": "R1"},
        "requires_verification": True,
        "valid_from": to_rfc3339(now - 60),
        "expires": to_rfc3339(now + 86400),
        "issued_by": "owner:ops",
    }
    core.sign_object(grant, "grant", ops)
    bundle = {
        "bundle": "2026.11.12-1",
        "issued_at": to_rfc3339(now),
        "policies": [
            {
                "id": "no-r3",
                "cedar": 'forbid (principal, action, resource) when { context.risk_class == "R3" };',
            }
        ],
        "groups": {},
        "grants": [grant],
        "signer": "owner:ops",
        "key": core.encode_public_key(ops),
    }
    bundle["signature"] = core.sign(ops, core.canonicalize(bundle))
    if tamper:
        bundle["grants"][0]["autonomy_level"] = "A4"
    (state / "policy-bundle.json").write_text(json.dumps(bundle))
    return bundle["bundle"]


def start(reboot, monkeypatch, tmp_path, **kw):
    state = tmp_path / "state"
    state.mkdir()
    version = authority(state, reboot.clock(), **kw)
    reboot.cfg = reboot.cfg.__class__(
        **{**reboot.cfg.__dict__, "control_state": state, "control_key": state / "node.key"}
    )
    (reboot.cfg.app_dir / "docker-compose.yml").write_text(COMPOSE)
    monkeypatch.setattr(reboot, "uptime", lambda: 3600.0)
    reboot.setup_governance()
    assert reboot.governance is not None
    return reboot, version


def test_bundle_is_verified_against_the_state_directory_registry(reboot, monkeypatch, tmp_path):
    b, version = start(reboot, monkeypatch, tmp_path)
    g = b.governance
    assert g.bundle_problem == ""
    assert g.pdp.bundle.version == version
    assert [x["grantee"] for x in g.pdp.bundle.grants] == ["agent:node-assistant"]


def test_without_a_registry_the_bundle_is_refused_and_the_baseline_stays(
    reboot, monkeypatch, tmp_path
):
    b, _ = start(reboot, monkeypatch, tmp_path, with_registry=False)
    assert "policy bundle refused" in b.governance.bundle_problem
    assert b.governance.pdp.bundle.grants == ()
    assert "policy bundle refused" in b.governance.audit_text()  # and /audit says so


def test_a_tampered_grant_is_refused(reboot, monkeypatch, tmp_path):
    b, _ = start(reboot, monkeypatch, tmp_path, tamper=True)
    assert "policy bundle refused" in b.governance.bundle_problem
    assert b.governance.pdp.bundle.grants == ()
