"""tools/ha_node.py: configure a HAOS node from the edge server. A fake node stands in for SSH."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import ha_node as hn

PKG = "packages/farm_soil.yaml"


class FakeNode:
    def __init__(self, files=None, backup_ok=True, has_ha=True):
        self.files = dict(files or {"configuration.yaml": b"default_config:\n"})
        self.files = {f"/config/{k}": v for k, v in self.files.items()}
        self.commands = []
        self.backup_ok = backup_ok
        self.has_ha = has_ha

    def run(self, command):
        self.commands.append(command)
        if command.startswith("ha backups new"):
            if not self.backup_ok:
                return 1, "boom"
            return 0, 'noise\n{"result":"ok","data":{"slug":"abc123"}}\n'
        if command == "command -v ha":
            return (0, "/usr/bin/ha") if self.has_ha else (1, "")
        return 0, "ok\n"

    def read(self, path):
        return self.files.get(path)

    def write(self, path, data):
        self.files[path] = data

    def delete(self, path):
        self.files.pop(path, None)


class FakeApi:
    def __init__(self, valid=True, up=True):
        self.valid, self.up, self.restarted = valid, up, False

    def ping(self):
        return self.up

    def check_config(self):
        return self.valid, "" if self.valid else "bad yaml"

    def restart(self):
        self.restarted = True


def sync(node, wanted, api=None, **kw):
    out = []
    api = api or FakeApi()
    ok = hn.do_sync(node, api, wanted, out=out.append, **kw)
    return ok, out, api


def test_first_sync_adds_files_and_writes_the_manifest():
    node = FakeNode()
    ok, out, _ = sync(node, {PKG: b"a: 1\n"})
    assert ok
    assert node.files["/config/" + PKG] == b"a: 1\n"
    m = json.loads(node.files[hn.MANIFEST])
    assert m["files"] == {PKG: hn.sha(b"a: 1\n")} and m["backup"] == "abc123"


def test_a_second_run_with_the_same_input_changes_nothing():
    node = FakeNode()
    sync(node, {PKG: b"a: 1\n"})
    node.commands.clear()
    ok, out, _ = sync(node, {PKG: b"a: 1\n"})
    assert ok and out == ["nothing to change"]
    assert not any(c.startswith("ha backups") for c in node.commands)


def test_our_own_file_is_updated_and_a_dropped_one_removed():
    node = FakeNode()
    sync(node, {PKG: b"a: 1\n", "packages/old.yaml": b"x: 1\n"})
    ok, _, _ = sync(node, {PKG: b"a: 2\n"})
    assert ok
    assert node.files["/config/" + PKG] == b"a: 2\n"
    assert "/config/packages/old.yaml" not in node.files


def test_a_file_we_do_not_own_is_never_overwritten():
    node = FakeNode({"configuration.yaml": b"x\n", PKG: b"mine: true\n"})
    with pytest.raises(hn.HaNodeError, match="not ours"):
        sync(node, {PKG: b"a: 1\n"})
    assert node.files["/config/" + PKG] == b"mine: true\n"
    assert not any(c.startswith("ha backups") for c in node.commands)


def test_a_file_edited_by_hand_since_our_sync_is_a_conflict():
    node = FakeNode()
    sync(node, {PKG: b"a: 1\n"})
    node.files["/config/" + PKG] = b"a: 1\n# my edit\n"
    with pytest.raises(hn.HaNodeError, match="edited by hand"):
        sync(node, {PKG: b"a: 2\n"})
    assert node.files["/config/" + PKG].endswith(b"my edit\n")


def test_invalid_config_rolls_everything_back_and_does_not_restart():
    node = FakeNode()
    sync(node, {PKG: b"a: 1\n"})
    before = dict(node.files)
    ok, out, api = sync(
        node,
        {PKG: b"a: broken\n", "packages/new.yaml": b"n: 1\n"},
        FakeApi(valid=False),
        restart=True,
    )
    assert not ok and node.files == before and not api.restarted
    assert any("previous files are back" in line for line in out)


def test_a_failing_backup_stops_before_anything_is_written():
    node = FakeNode(backup_ok=False)
    with pytest.raises(hn.HaNodeError, match="backup failed"):
        sync(node, {PKG: b"a: 1\n"})
    assert "/config/" + PKG not in node.files


def test_restart_only_when_asked_and_valid():
    node = FakeNode()
    _, _, api = sync(node, {PKG: b"a: 1\n"})
    assert not api.restarted
    node2 = FakeNode()
    _, _, api2 = sync(node2, {PKG: b"a: 1\n"}, restart=True)
    assert api2.restarted


def test_the_packages_include_is_added_only_when_asked_and_safe():
    node = FakeNode()
    sync(node, {PKG: b"a: 1\n"})
    assert b"packages:" not in node.files["/config/configuration.yaml"]
    node2 = FakeNode()
    sync(node2, {PKG: b"a: 1\n"}, add_include=True)
    assert (
        node2.files["/config/configuration.yaml"]
        == b"default_config:\nhomeassistant:\n  packages: !include_dir_named packages\n"
    )


def test_an_existing_homeassistant_block_is_left_alone_with_instructions():
    cfg = b"homeassistant:\n  name: Farm\n"
    node = FakeNode({"configuration.yaml": cfg})
    ok, out, _ = sync(node, {PKG: b"a: 1\n"}, add_include=True)
    assert ok and node.files["/config/configuration.yaml"] == cfg
    assert any("packages: !include_dir_named packages" in line for line in out)


def test_a_failure_part_way_through_restores_the_files():
    class Flaky(FakeNode):
        def write(self, path, data):
            if path.endswith("two.yaml"):
                raise hn.HaNodeError("disk full")
            super().write(path, data)

    node = Flaky()
    with pytest.raises(hn.HaNodeError):
        sync(node, {"packages/one.yaml": b"1\n", "packages/two.yaml": b"2\n"})
    assert "/config/packages/one.yaml" not in node.files


def test_check_reports_each_problem():
    out = []
    assert hn.do_check(FakeNode(), FakeApi(), out.append)
    out.clear()
    assert not hn.do_check(FakeNode(has_ha=False), FakeApi(up=False), out.append)
    text = "\n".join(out)
    assert "NOT FOUND: install Advanced SSH" in text and "FAILED (wrong URL or token)" in text


def test_source_reads_only_allowed_roots_and_refuses_symlinks(tmp_path):
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages/a.yaml").write_text("a: 1\n")
    (tmp_path / "packages/.hidden").write_text("no")
    (tmp_path / "configuration.yaml").write_text("never copied")
    (tmp_path / "secrets.yaml").write_text("never copied")
    assert hn.read_source(tmp_path) == {"packages/a.yaml": b"a: 1\n"}
    (tmp_path / "packages/link.yaml").symlink_to(tmp_path / "secrets.yaml")
    with pytest.raises(hn.HaNodeError, match="symbolic links"):
        hn.read_source(tmp_path)


def test_a_damaged_manifest_stops_the_sync():
    node = FakeNode()
    node.files[hn.MANIFEST] = b"not json"
    with pytest.raises(hn.HaNodeError, match="damaged"):
        sync(node, {PKG: b"a: 1\n"})


def test_init_writes_a_site_file_without_secrets(tmp_path):
    out = tmp_path / "ha-node.yaml"
    assert (
        hn.main(["init", "--out", str(out), "--host", "ha.local", "--token-file", "/etc/ha.token"])
        == 0
    )
    text = out.read_text()
    assert "host: ha.local" in text and "token_file: /etc/ha.token" in text
    assert hn.load_site(str(out))["host"] == "ha.local"


def test_http_api_ping_and_check(monkeypatch):
    calls = []

    def call(self, method, path, body=None):
        calls.append((method, path))
        return (
            {"message": "API running."} if path == "/api/" else {"result": "invalid", "errors": "e"}
        )

    monkeypatch.setattr(hn.HttpApi, "_call", call)
    api = hn.HttpApi("http://ha:8123/", "tok")
    assert api.ping() and api.check_config() == (False, "e")
