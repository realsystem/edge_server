"""Tests for snapshot module."""

import io
import json
import shlex
import tarfile
from unittest.mock import MagicMock

import pytest

from snapshot import LocalSnapshotManager, SnapshotManager, SnapshotManifest
from ssh import SSHResult

OK = SSHResult(0, "", "")
FAIL = SSHResult(1, "", "error")


def manifest_json(**overrides):
    data = {
        "id": "2026-01-01T00:00:00_deploy",
        "created_at": "2026-01-01T00:00:00",
        "phase": "deploy",
        "description": "Before deployment",
        "files": ["/opt/edge-server"],
        "docker_containers": ["mosquitto", "frigate"],
        "metadata": {},
    }
    data.update(overrides)
    return json.dumps(data)


class FakeSSH:
    """Records commands; answers from a list of (substring, result) rules."""

    def __init__(self, rules=None):
        self.calls = []
        self.rules = rules or []

    def run(self, command, timeout=None, sudo=False, input=None):
        self.calls.append({"command": command, "sudo": sudo, "input": input})
        for needle, result in self.rules:
            if needle in command:
                return result
        return OK

    def path_exists(self, path):
        return True

    def commands(self):
        return [c["command"] for c in self.calls]


class TestSnapshotManagerCreate:
    def test_create_writes_manifest_via_sudo_tee(self):
        ssh = FakeSSH(rules=[("docker ps", SSHResult(0, "mosquitto\nfrigate", ""))])
        mgr = SnapshotManager(ssh, "/var/lib/snaps")

        snap_id = mgr.create("deploy", "Before", paths=["/opt/edge-server"], include_docker=True)

        assert snap_id is not None and snap_id.endswith("_deploy")
        tee_calls = [c for c in ssh.calls if c["command"].startswith("tee ")]
        manifest_call = [c for c in tee_calls if "manifest.json" in c["command"]][0]
        # Written as root via stdin, not via a user-side shell redirect
        assert manifest_call["sudo"] is True
        data = json.loads(manifest_call["input"])
        assert data["files"] == ["/opt/edge-server"]
        assert data["docker_containers"] == ["mosquitto", "frigate"]
        # one inspect dump per container
        assert len(tee_calls) == 3
        assert any("ln -sfn" in c and "/var/lib/snaps/latest" in c for c in ssh.commands())

    def test_create_quotes_paths(self):
        ssh = FakeSSH()
        SnapshotManager(ssh).create("p", "d", paths=["/etc/my dir;rm -rf x"])
        tar_cmd = [c for c in ssh.commands() if c.startswith("tar -czf")][0]
        argv = shlex.split(tar_cmd)
        assert argv[-3:] == ["-C", "/etc", "my dir;rm -rf x"]

    def test_create_returns_none_when_mkdir_fails(self):
        ssh = FakeSSH(rules=[("mkdir -p", FAIL)])
        assert SnapshotManager(ssh).create("p", "d") is None

    def test_create_returns_none_when_manifest_write_fails(self):
        ssh = FakeSSH(rules=[("manifest.json", FAIL)])
        assert SnapshotManager(ssh).create("p", "d") is None

    def test_uses_configured_snapshot_dir(self):
        ssh = FakeSSH()
        SnapshotManager(ssh, "/data/snaps/").create("p", "d")
        assert ssh.commands()[0].startswith("mkdir -p /data/snaps/")


class TestSnapshotManagerRestore:
    def test_restore_latest(self):
        ssh = FakeSSH(
            rules=[
                ("readlink", SSHResult(0, "/var/lib/edge-server-snapshots/snap1", "")),
                ("cat ", SSHResult(0, manifest_json(id="snap1"), "")),
            ]
        )
        assert SnapshotManager(ssh).restore("latest") is True
        cmds = ssh.commands()
        stop = cmds.index("docker stop mosquitto 2>/dev/null || true")
        extract = next(i for i, c in enumerate(cmds) if c.startswith("tar -xzf"))
        start = cmds.index("docker start mosquitto 2>/dev/null || true")
        assert stop < extract < start  # containers restarted after files restored
        assert "/var/lib/edge-server-snapshots/snap1/edge-server.tar.gz" in cmds[extract]

    def test_restore_reports_extract_failure(self):
        ssh = FakeSSH(rules=[("cat ", SSHResult(0, manifest_json(), "")), ("tar -xzf", FAIL)])
        assert SnapshotManager(ssh).restore("x") is False

    def test_restore_reports_missing_archive(self):
        ssh = FakeSSH(rules=[("cat ", SSHResult(0, manifest_json(), "")), ("test -f", FAIL)])
        assert SnapshotManager(ssh).restore("x") is False
        assert not any(c.startswith("rm -rf") for c in ssh.commands())

    def test_restore_missing_manifest(self):
        ssh = FakeSSH(rules=[("cat ", FAIL)])
        assert SnapshotManager(ssh).restore("nope") is False

    def test_corrupt_manifest(self):
        ssh = FakeSSH(rules=[("cat ", SSHResult(0, "{not json", ""))])
        assert SnapshotManager(ssh).get_manifest("x") is None


class TestSnapshotManagerHousekeeping:
    def test_list_and_cleanup(self):
        listing = SSHResult(0, "a\nb\nc", "")
        manifests = {
            n: SSHResult(0, manifest_json(id=n, created_at=f"2026-01-0{i}"), "")
            for i, n in enumerate("abc", start=1)
        }
        rules = [("ls -1", listing)] + [(f"/{n}/manifest.json", r) for n, r in manifests.items()]
        ssh = FakeSSH(rules=rules)
        mgr = SnapshotManager(ssh)
        assert [s.id for s in mgr.list_snapshots()] == ["c", "b", "a"]
        assert mgr.cleanup_old(keep=1) == 2
        assert sum(c.startswith("rm -rf") for c in ssh.commands()) == 2

    @pytest.mark.parametrize("bad", ["", "latest", "..", "../etc", "a/b"])
    def test_delete_rejects_unsafe_ids(self, bad):
        ssh = FakeSSH()
        assert SnapshotManager(ssh).delete(bad) is False
        assert ssh.calls == []

    def test_delete(self):
        ssh = FakeSSH()
        assert SnapshotManager(ssh).delete("snap1") is True


class TestLocalSnapshotManager:
    def test_roundtrip(self, tmp_path):
        target = tmp_path / "data"
        target.mkdir()
        (target / "file.txt").write_text("original")
        mgr = LocalSnapshotManager(tmp_path / "snaps")

        snap_id = mgr.create("test", "desc", paths=[target, tmp_path / "missing"])
        assert snap_id
        manifest = json.loads((tmp_path / "snaps" / snap_id / "manifest.json").read_text())
        assert manifest["files"] == [str(target)]
        assert SnapshotManifest(**manifest).phase == "test"

        (target / "file.txt").write_text("changed")
        (target / "new.txt").write_text("new")

        assert mgr.restore("latest") is True
        assert (target / "file.txt").read_text() == "original"
        assert not (target / "new.txt").exists()

    def test_restore_by_id_and_missing(self, tmp_path):
        mgr = LocalSnapshotManager(tmp_path / "snaps")
        assert mgr.restore("latest") is False
        assert mgr.restore("nope") is False
        snap_id = mgr.create("x", "y")
        assert mgr.restore(snap_id) is True

    def test_latest_symlink_updated(self, tmp_path):
        mgr = LocalSnapshotManager(tmp_path / "snaps")
        latest = tmp_path / "snaps" / "latest"
        first = mgr.create("one", "d")
        assert latest.resolve().name == first
        second = mgr.create("two", "d")
        assert latest.resolve().name == second

    def test_restore_rejects_path_traversal(self, tmp_path):
        snaps = tmp_path / "snaps"
        snap = snaps / "evil"
        snap.mkdir(parents=True)
        target = tmp_path / "victim" / "data"
        target.parent.mkdir()
        with tarfile.open(snap / "data.tar.gz", "w:gz") as tar:
            payload = b"pwned"
            info = tarfile.TarInfo("../../escaped.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        (snap / "manifest.json").write_text(
            manifest_json(id="evil", files=[str(target)], docker_containers=[])
        )
        with pytest.raises((tarfile.TarError, ValueError)):
            LocalSnapshotManager(snaps).restore("evil")
        assert not (tmp_path / "escaped.txt").exists()


def test_snapshot_manager_mock_ssh_contract():
    """SnapshotManager only relies on run()/path_exists() of the SSH client."""
    ssh = MagicMock()
    ssh.run.return_value = OK
    ssh.path_exists.return_value = False
    assert SnapshotManager(ssh).create("p", "d") is not None
