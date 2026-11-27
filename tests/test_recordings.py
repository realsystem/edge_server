"""Moving Frigate's recordings when the storage folder changes, and publishing them on OMV.

lib/recordings.sh (migrate_recordings, share_recordings) and host/omv-share-recordings.py.
docker, systemctl and the OMV tools are stubs; rsync is the real one (tests skip without it).
"""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
RSYNC = shutil.which("rsync")

spec = importlib.util.spec_from_file_location(
    "omv_share", REPO / "host" / "omv-share-recordings.py"
)
omv_share = importlib.util.module_from_spec(spec)
sys.modules["omv_share"] = omv_share
spec.loader.exec_module(omv_share)


# --------------------------------------------------------------------------
# lib/recordings.sh
# --------------------------------------------------------------------------

LOGGING = '#!/usr/bin/env bash\necho "{name} $*" >> "$STUB_LOG"\nexit "${{{rc}:-0}}"\n'


@pytest.fixture
def box(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, rc in (("systemctl", "SYSTEMCTL_RC"), ("docker", "DOCKER_RC")):
        (bin_dir / name).write_text(LOGGING.format(name=name, rc=rc))
        (bin_dir / name).chmod(0o755)
    (tmp_path / "log").write_text("")
    (tmp_path / "app").mkdir()
    old = tmp_path / "old" / "frigate"
    (old / "recordings" / "2026-11-27" / "10" / "cam").mkdir(parents=True)
    (old / "recordings" / "2026-11-27" / "10" / "cam" / "00.00.mp4").write_text("video-1")
    (old / "recordings" / "2026-11-27" / "10" / "cam" / "00.10.mp4").write_text("video-2")
    (old / "clips").mkdir()
    (tmp_path / "app" / "docker-compose.yml").write_text(
        "services:\n  frigate:\n    volumes:\n"
        f"      - {old}:/media/frigate\n      - /etc/localtime:/etc/localtime:ro\n"
        f"  telegram-bot:\n    volumes:\n      - {old}:/media/frigate:ro\n"
    )
    return tmp_path


def sh(box, body, input=None, **env):
    full = {
        "PATH": f"{box / 'bin'}:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/tmp"),
        "STUB_LOG": str(box / "log"),
        "EDGE_SERVER_DIR": str(box / "app"),
        "EDGE_STORAGE_DIR": str(box / "new"),
        "BATCH_MODE": "true",
    }
    full.update({k: str(v) for k, v in env.items()})
    prelude = (
        f"source {shlex.quote(str(REPO / 'deploy-edge-server.sh'))}\n"
        "log() { printf '%s\\n' \"$*\"; }\n"
        "info() { printf '%s\\n' \"$*\"; }; warn() { printf '%s\\n' \"$*\"; }\n"
        "success() { printf '%s\\n' \"$*\"; }; fatal() { printf 'FATAL %s\\n' \"$*\"; exit 1; }\n"
    )
    return subprocess.run(
        [BASH, "-c", prelude + body],
        capture_output=True,
        text=True,
        env=full,
        timeout=60,
        input=input,
    )


def log(box):
    return (box / "log").read_text()


needs_rsync = pytest.mark.skipif(RSYNC is None or BASH is None, reason="bash and rsync needed")


@pytest.mark.skipif(BASH is None, reason="bash not available")
class TestCurrentRecordingsDir:
    def test_reads_the_rw_mount_not_the_bots_read_only_one(self, box):
        r = sh(box, "current_recordings_dir")
        assert r.stdout.strip() == str(box / "old" / "frigate")

    def test_nothing_without_a_compose_file(self, box):
        (box / "app" / "docker-compose.yml").unlink()
        assert sh(box, "current_recordings_dir").stdout.strip() == ""


@needs_rsync
class TestMigrateRecordings:
    NEW = "new/frigate"

    def moved(self, box):
        return (box / self.NEW / "recordings" / "2026-11-27" / "10" / "cam" / "00.00.mp4").exists()

    def test_moves_when_asked_and_keeps_the_old_copy(self, box):
        r = sh(box, "migrate_recordings", STORAGE_MIGRATE="true")
        assert r.returncode == 0, r.stdout + r.stderr
        assert self.moved(box)
        old = box / "old" / "frigate" / "recordings" / "2026-11-27" / "10" / "cam" / "00.10.mp4"
        assert old.read_text() == "video-2"
        calls = log(box)
        assert "systemctl stop edge-server-health.timer" in calls  # or it would restart the stack
        assert "docker compose stop" in calls
        assert "kept" in r.stdout

    def test_does_nothing_when_the_folder_is_the_same(self, box):
        r = sh(box, "migrate_recordings", STORAGE_MIGRATE="true", EDGE_STORAGE_DIR=box / "old")
        assert r.returncode == 0 and not (box / self.NEW).exists()
        assert log(box) == ""

    def test_batch_mode_leaves_them_with_a_warning(self, box):
        r = sh(box, "migrate_recordings")
        assert "STORAGE_MIGRATE=true" in r.stdout
        assert not self.moved(box) and log(box) == ""

    def test_false_never_moves(self, box):
        r = sh(box, "migrate_recordings", STORAGE_MIGRATE="false")
        assert "stay in" in r.stdout and not self.moved(box)

    def test_bad_value_leaves_them(self, box):
        r = sh(box, "migrate_recordings", STORAGE_MIGRATE="maybe")
        assert "must be true or false" in r.stdout and not self.moved(box)

    def test_manual_run_asks(self, box):
        r = sh(box, "migrate_recordings", input="y\n", BATCH_MODE="false")
        assert self.moved(box), r.stdout + r.stderr
        box2 = box / "again"
        box2.mkdir()
        r = sh(box, "migrate_recordings", input="n\n", BATCH_MODE="false", EDGE_STORAGE_DIR=box2)
        assert not (box2 / "frigate").exists()

    def test_old_folder_without_recordings_is_not_a_move(self, box):
        shutil.rmtree(box / "old" / "frigate" / "recordings")
        (box / "old" / "frigate" / "recordings").mkdir()
        r = sh(box, "migrate_recordings", STORAGE_MIGRATE="true")
        assert "no recordings to move" in r.stdout and log(box) == ""

    def test_nested_folders_are_refused(self, box):
        inner = box / "old" / "frigate" / "sub"
        r = sh(box, "migrate_recordings", STORAGE_MIGRATE="true", EDGE_STORAGE_DIR=inner)
        assert "inside the old one" in r.stdout and log(box) == ""

    def test_not_enough_space_skips_before_stopping_anything(self, box):
        (box / "bin" / "df").write_text("#!/usr/bin/env bash\nprintf 'Avail\\n1\\n'\n")
        (box / "bin" / "df").chmod(0o755)
        r = sh(box, "migrate_recordings", STORAGE_MIGRATE="true")
        assert "Not enough free space" in r.stdout and not self.moved(box)
        assert "docker compose stop" not in log(box)

    def test_a_failed_copy_starts_the_old_stack_again(self, box):
        (box / "bin" / "rsync").write_text("#!/usr/bin/env bash\nexit 23\n")
        (box / "bin" / "rsync").chmod(0o755)
        r = sh(box, "migrate_recordings", STORAGE_MIGRATE="true")
        assert (
            r.returncode == 1
            and "FATAL" in r.stdout
            and "untouched" in r.stdout.replace("nothing was changed", "untouched")
        )
        calls = log(box)
        assert "docker compose stop" in calls and "docker compose start" in calls
        assert "systemctl start edge-server-health.timer" in calls

    def test_deploy_runs_it_between_storage_and_the_new_stack(self):
        text = (REPO / "deploy-edge-server.sh").read_text()
        body = text[text.index("run_deploy() {") : text.index("main() {")]
        assert body.index("configure_storage") < body.index("migrate_recordings")
        assert body.index("migrate_recordings") < body.index("create_app_stack")
        assert body.index("install_md_alert") < body.index("share_recordings")


@pytest.mark.skipif(BASH is None, reason="bash not available")
class TestShareRecordingsStep:
    def test_off_by_default(self, box):
        assert sh(box, "share_recordings").stdout.strip() == ""

    def test_skipped_when_not_openmediavault(self, box):
        r = sh(box, "share_recordings", FRIGATE_SHARE="true", HOST_PLATFORM="generic")
        assert "not on this machine" in r.stdout

    def test_bad_values_are_warnings(self, box):
        assert "must be true or false" in sh(box, "share_recordings", FRIGATE_SHARE="x").stdout
        r = sh(
            box,
            "share_recordings",
            FRIGATE_SHARE="true",
            HOST_PLATFORM="omv",
            FRIGATE_SHARE_CLIENT="not-an-ip",
        )
        assert "not an IPv4" in r.stdout

    def test_passes_the_path_and_the_network_to_the_script(self, box):
        (box / "bin" / "python3").write_text(
            '#!/usr/bin/env bash\necho "python3 $*" >> "$STUB_LOG"\necho created\n'
        )
        (box / "bin" / "python3").chmod(0o755)
        r = sh(
            box,
            "share_recordings",
            FRIGATE_SHARE="true",
            HOST_PLATFORM="omv",
            LOCAL_SUBNET="192.168.7.0/24",
        )
        assert r.returncode == 0, r.stdout + r.stderr
        call = log(box)
        assert "omv-share-recordings.py" in call
        assert f"--path {box / 'new' / 'frigate'}" in call and "--client 192.168.7.0/24" in call
        # an explicit client wins over the detected network
        (box / "log").write_text("")
        sh(
            box,
            "share_recordings",
            FRIGATE_SHARE="true",
            HOST_PLATFORM="omv",
            LOCAL_SUBNET="192.168.7.0/24",
            FRIGATE_SHARE_CLIENT="10.1.2.3",
        )
        assert "--client 10.1.2.3" in log(box)

    def test_a_failing_script_is_a_warning_not_a_failed_deploy(self, box):
        (box / "bin" / "python3").write_text("#!/usr/bin/env bash\necho boom >&2\nexit 1\n")
        (box / "bin" / "python3").chmod(0o755)
        r = sh(
            box,
            "share_recordings",
            FRIGATE_SHARE="true",
            HOST_PLATFORM="omv",
            LOCAL_SUBNET="10.0.0.0/24",
        )
        assert r.returncode == 0 and "Could not share the recordings" in r.stdout

    def test_settings_are_saved_for_auto(self, box):
        r = sh(
            box,
            "save_settings; cat $SETTINGS_FILE",
            FRIGATE_SHARE="true",
            FRIGATE_SHARE_CLIENT="10.0.0.0/24",
            STORAGE_MIGRATE="true",
        )
        assert "FRIGATE_SHARE=true" in r.stdout and "FRIGATE_SHARE_CLIENT=10.0.0.0/24" in r.stdout
        assert "STORAGE_MIGRATE" not in r.stdout  # a one-time action, not a setting to repeat


# --------------------------------------------------------------------------
# host/omv-share-recordings.py
# --------------------------------------------------------------------------

MAIN = "d3feb80e-7473-410b-92b9-6cc640a58bab"
SDD = "f8066686-c1f3-40ce-a9db-260ce8c426ad"


class FakeOmv:
    """Just enough of omv-confdbadm and omv-rpc, with the pending-changes file."""

    def __init__(self, tmp_path, dirty_before=False, nfs_enabled=True):
        self.dirty_file = tmp_path / "dirtymodules.json"
        self.dirty_file.write_text(json.dumps(["monit"] if dirty_before else []))
        self.mounts = [
            {
                "uuid": MAIN,
                "dir": "/srv/dev-disk-by-label-main",
                "fsname": "/dev/disk/by-label/main",
            },
            {"uuid": SDD, "dir": "/srv/dev-disk-by-id-x", "fsname": "/dev/disk/by-id/x"},
            {
                "uuid": "b1",
                "dir": "/export/yt_video",
                "fsname": "/srv/dev-disk-by-label-main/yt_video/",
            },
        ]
        self.folders = [
            {"uuid": "yt", "name": "yt_video", "mntentref": MAIN, "reldirpath": "yt_video/"}
        ]
        self.nfs = {"enable": nfs_enabled, "shares": {"share": []}}
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd[:5] if cmd[0] == "omv-rpc" else cmd[:3])
        if cmd[0] == "omv-confdbadm":
            return json.dumps(
                {
                    "conf.system.filesystem.mountpoint": self.mounts,
                    "conf.system.sharedfolder": self.folders,
                    "conf.service.nfs": self.nfs,
                }[cmd[2]]
            )
        service, method, params = cmd[3], cmd[4], json.loads(cmd[5])
        if (service, method) == ("ShareMgmt", "set"):
            created = dict(params, uuid="sf-new-uuid")
            self.folders.append(created)
            self._dirty(["sharedfolders", "fstab"])
            return json.dumps(created)
        if (service, method) == ("Nfs", "setShare"):
            self.nfs["shares"]["share"].append(dict(params, uuid="nfs-new"))
            self._dirty(["nfs"])
            return "{}"
        if (service, method) == ("Config", "applyChanges"):
            self.dirty_file.write_text("[]")
            self.applied = params["modules"]
            return "null"
        raise AssertionError(cmd)

    def _dirty(self, mods):
        cur = json.loads(self.dirty_file.read_text())
        self.dirty_file.write_text(json.dumps(cur + [m for m in mods if m not in cur]))


@pytest.fixture
def omv(tmp_path, monkeypatch):
    fake = FakeOmv(tmp_path)
    monkeypatch.setattr(omv_share, "DIRTY_FILE", str(fake.dirty_file))
    return fake


PATH = "/srv/dev-disk-by-label-main/frigate"


class TestOmvShare:
    def test_creates_the_folder_and_a_read_only_share_and_applies(self, omv):
        res = omv_share.publish(PATH, "10.0.0.0/24", "frigate", omv)
        assert res["shared_folder"].startswith("created") and res["nfs"].startswith("created")
        assert res["applied"] is True
        folder = omv.folders[-1]
        assert folder["mntentref"] == MAIN and folder["reldirpath"] == "frigate/"
        share = omv.nfs["shares"]["share"][0]
        assert share["options"] == "ro" and share["client"] == "10.0.0.0/24"
        assert share["sharedfolderref"] == "sf-new-uuid"
        assert set(omv.applied) == {"sharedfolders", "fstab", "nfs"}

    def test_a_second_run_changes_nothing(self, omv):
        omv_share.publish(PATH, "10.0.0.0/24", "frigate", omv)
        n = len(omv.calls)
        res = omv_share.publish(PATH, "10.0.0.0/24", "frigate", omv)
        assert res["shared_folder"].startswith("reused") and res["nfs"].startswith("reused")
        assert res["applied"] is False
        assert not any(c[3:5] in (["ShareMgmt", "set"], ["Nfs", "setShare"]) for c in omv.calls[n:])

    def test_an_existing_shared_folder_at_that_place_is_reused_under_its_own_name(self, omv):
        omv.folders.append(
            {"uuid": "mine", "name": "cameras", "mntentref": MAIN, "reldirpath": "frigate/"}
        )
        res = omv_share.publish(PATH, "10.0.0.0/24", "frigate", omv)
        assert "cameras" in res["shared_folder"]
        assert omv.nfs["shares"]["share"][0]["sharedfolderref"] == "mine"

    def test_a_name_used_elsewhere_is_refused(self, omv):
        omv.folders.append(
            {"uuid": "x", "name": "frigate", "mntentref": MAIN, "reldirpath": "other/"}
        )
        with pytest.raises(RuntimeError, match="already points somewhere else"):
            omv_share.publish(PATH, "10.0.0.0/24", "frigate", omv)

    def test_other_waiting_changes_are_not_applied_behind_the_users_back(
        self, tmp_path, monkeypatch
    ):
        fake = FakeOmv(tmp_path, dirty_before=True)
        monkeypatch.setattr(omv_share, "DIRTY_FILE", str(fake.dirty_file))
        res = omv_share.publish(PATH, "10.0.0.0/24", "frigate", fake)
        assert res["applied"] is False and "press Apply" in " ".join(res["notes"])
        assert not hasattr(fake, "applied")

    def test_a_disabled_nfs_service_is_pointed_out(self, tmp_path, monkeypatch):
        fake = FakeOmv(tmp_path, nfs_enabled=False)
        monkeypatch.setattr(omv_share, "DIRTY_FILE", str(fake.dirty_file))
        res = omv_share.publish(PATH, "10.0.0.0/24", "frigate", fake)
        assert any("switched off" in n for n in res["notes"])

    def test_the_folder_must_be_on_an_omv_file_system_and_inside_it(self, omv):
        with pytest.raises(RuntimeError, match="not on a file system"):
            omv_share.publish("/mnt/storage/frigate", "10.0.0.0/24", "frigate", omv)
        with pytest.raises(RuntimeError, match="not its root"):
            omv_share.publish("/srv/dev-disk-by-label-main", "10.0.0.0/24", "frigate", omv)

    def test_bad_input_is_refused_before_anything_is_changed(self, omv):
        for args, msg in (
            (("rel/path", "10.0.0.0/24", "frigate"), "absolute"),
            ((PATH, "everyone", "frigate"), "IPv4"),
            ((PATH, "10.0.0.0/24", "bad name"), "share name"),
        ):
            with pytest.raises(RuntimeError, match=msg):
                omv_share.publish(*args, omv)
        assert omv.calls == []

    def test_rpc_errors_surface(self, omv):
        def failing(cmd):
            if cmd[0] == "omv-rpc":
                return json.dumps({"response": None, "error": {"message": "boom"}})
            return omv(cmd)

        with pytest.raises(RuntimeError, match="boom"):
            omv_share.publish(PATH, "10.0.0.0/24", "frigate", failing)

    def test_main_prints_a_summary_and_returns_an_exit_code(self, omv, monkeypatch, capsys):
        monkeypatch.setattr(omv_share, "run", omv)
        assert omv_share.main(["--path", PATH, "--client", "10.0.0.0/24"]) == 0
        out = capsys.readouterr().out
        assert "NFS share:     created" in out and "applied:       yes" in out
        assert omv_share.main(["--path", "nope", "--client", "10.0.0.0/24"]) == 1
        assert "absolute" in capsys.readouterr().err
