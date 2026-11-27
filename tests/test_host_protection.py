"""Optional host protection: the nightly config backup and the RAID event alert.

lib/protect.sh (installed by deploy-edge-server.sh) and the two scripts in host/. systemctl and
mdadm are stubs on PATH; every path is redirected into a temporary directory.
"""

import os
import shlex
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

SYSTEMCTL = '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\nexit "${SYSTEMCTL_RC:-0}"\n'
MDADM = "#!/usr/bin/env bash\nexit 0\n"


@pytest.fixture
def box(tmp_path):
    """A fake machine: stubs on PATH, an app folder, a unit folder, a backup target."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("systemctl", SYSTEMCTL), ("mdadm", MDADM)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (tmp_path / "log").write_text("")
    app = tmp_path / "app"
    app.mkdir()
    (tmp_path / "units").mkdir()
    return tmp_path


def sh(box, body, **env):
    full = {
        "PATH": f"{box / 'bin'}:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/tmp"),
        "STUB_LOG": str(box / "log"),
        "EDGE_SERVER_DIR": str(box / "app"),
        "SYSTEMD_DIR": str(box / "units"),
        "MDSTAT_FILE": str(box / "mdstat"),
        "MDADM_CONF": str(box / "mdadm.conf"),
    }
    full.update({k: str(v) for k, v in env.items()})
    prelude = (
        f"source {shlex.quote(str(REPO / 'deploy-edge-server.sh'))}\n"
        "log() { printf '%s\\n' \"$*\"; }\n"
        "info() { printf '%s\\n' \"$*\"; }; warn() { printf '%s\\n' \"$*\"; }\n"
        "success() { printf '%s\\n' \"$*\"; }\n"
    )
    return subprocess.run(
        [BASH, "-c", prelude + body], capture_output=True, text=True, env=full, timeout=60
    )


def log(box):
    return (box / "log").read_text()


class TestConfigBackupInstall:
    def test_off_by_default_installs_nothing(self, box):
        r = sh(box, "install_config_backup")
        assert r.returncode == 0, r.stdout + r.stderr
        assert list((box / "units").iterdir()) == []
        assert not (box / "app" / "config-backup.sh").exists()

    def test_installs_script_timer_and_runs_it_once(self, box):
        dest = box / "backups"
        r = sh(box, "install_config_backup", CONFIG_BACKUP_DIR=dest, CONFIG_BACKUP_KEEP=5)
        assert r.returncode == 0, r.stdout + r.stderr
        script = box / "app" / "config-backup.sh"
        assert script.exists() and stat.S_IMODE(script.stat().st_mode) == 0o755
        service = (box / "units" / "edge-config-backup.service").read_text()
        assert f"ExecStart={script} {dest} 5" in service
        assert f"Environment=EDGE_SERVER_DIR={box / 'app'}" in service
        timer = (box / "units" / "edge-config-backup.timer").read_text()
        assert "OnCalendar=*-*-* 03:30:00" in timer and "Persistent=true" in timer
        calls = log(box)
        assert "enable --now edge-config-backup.timer" in calls
        assert "start edge-config-backup.service" in calls

    def test_bad_paths_and_values_are_refused_without_failing_the_deploy(self, box):
        for bad in ("relative/dir", "/srv/with space/x", "/srv/../etc", str(box / "app" / "b")):
            r = sh(box, "install_config_backup", CONFIG_BACKUP_DIR=bad)
            assert r.returncode == 0, bad
            assert "no config backup" in r.stdout, bad
            assert list((box / "units").iterdir()) == [], bad
        r = sh(box, "install_config_backup", CONFIG_BACKUP_DIR=box / "b", CONFIG_BACKUP_KEEP="x")
        assert "using 14" in r.stdout
        assert (
            f"config-backup.sh {box / 'b'} 14"
            in (box / "units" / "edge-config-backup.service").read_text()
        )

    def test_empty_setting_removes_only_what_it_installed(self, box):
        sh(box, "install_config_backup", CONFIG_BACKUP_DIR=box / "b")
        r = sh(box, "install_config_backup")
        assert "switched off" in r.stdout
        assert list((box / "units").iterdir()) == []
        assert not (box / "app" / "config-backup.sh").exists()
        # a timer with the same name that the user wrote is left alone
        (box / "units" / "edge-config-backup.timer").write_text("[Timer]\nOnCalendar=daily\n")
        sh(box, "install_config_backup")
        assert (box / "units" / "edge-config-backup.timer").exists()

    def test_the_setting_is_saved_for_auto(self, box):
        r = sh(
            box,
            "save_settings; cat $SETTINGS_FILE",
            CONFIG_BACKUP_DIR="/srv/raid/backup",
            CONFIG_BACKUP_KEEP=7,
            MD_ALERT="true",
        )
        assert "CONFIG_BACKUP_DIR=/srv/raid/backup" in r.stdout
        assert "CONFIG_BACKUP_KEEP=7" in r.stdout and "MD_ALERT=true" in r.stdout

    def test_deploy_runs_the_steps(self):
        text = (REPO / "deploy-edge-server.sh").read_text()
        body = text[text.index("run_deploy() {") : text.index("main() {")]
        assert body.index("install_host_helpers") < body.index("install_config_backup")
        assert body.index("install_config_backup") < body.index("install_md_alert")
        assert body.index("install_md_alert") < body.index("start_services")


class TestMdAlertInstall:
    def raid(self, box, conf=""):
        (box / "mdstat").write_text("Personalities : [raid1]\nmd0 : active raid1 sdb[1] sdc[0]\n")
        (box / "mdadm.conf").write_text(conf)

    def test_off_by_default(self, box):
        self.raid(box)
        r = sh(box, "install_md_alert")
        assert r.returncode == 0
        assert not (box / "units" / "mdmonitor.service.d").exists()

    def test_installs_the_override_where_it_applies(self, box):
        self.raid(box, conf="ARRAY /dev/md0 metadata=1.2 UUID=abc\n")
        r = sh(box, "install_md_alert", MD_ALERT="true")
        assert r.returncode == 0, r.stdout + r.stderr
        override = (box / "units" / "mdmonitor.service.d" / "edge-server.conf").read_text()
        lines = override.splitlines()
        assert lines.count("ExecStart=") == 1  # clears the packaged command first
        assert any(
            line.startswith("ExecStart=") and "--monitor --scan --syslog" in line for line in lines
        )
        assert f"--program={box / 'app' / 'md-alert.sh'}" in override
        assert (box / "app" / "md-alert.sh").exists()
        assert "restart mdmonitor" in log(box)

    def test_skipped_without_software_raid(self, box):
        (box / "mdstat").write_text("Personalities :\nunused devices: <none>\n")
        r = sh(box, "install_md_alert", MD_ALERT="true")
        assert r.returncode == 0 and "no software RAID" in r.stdout
        assert not (box / "units" / "mdmonitor.service.d").exists()

    def test_skipped_when_mdadm_already_has_a_target(self, box):
        for conf in ("MAILADDR admin@example.com\n", "PROGRAM /usr/local/bin/x\n"):
            self.raid(box, conf=conf)
            r = sh(box, "install_md_alert", MD_ALERT="true")
            assert "already has a MAILADDR or PROGRAM" in r.stdout
            assert not (box / "units" / "mdmonitor.service.d").exists()
        # a commented-out one is not a target
        self.raid(box, conf="#MAILADDR root\n")
        assert (
            "Installing the RAID event alert" in sh(box, "install_md_alert", MD_ALERT="true").stdout
        )

    def test_bad_value_is_a_warning_and_turning_it_off_removes_only_ours(self, box):
        self.raid(box)
        assert "must be true or false" in sh(box, "install_md_alert", MD_ALERT="yes").stdout
        sh(box, "install_md_alert", MD_ALERT="true")
        r = sh(box, "install_md_alert", MD_ALERT="false")
        assert "switched off" in r.stdout
        assert not (box / "units" / "mdmonitor.service.d").exists()
        assert not (box / "app" / "md-alert.sh").exists()
        # an override that is not ours stays
        d = box / "units" / "mdmonitor.service.d"
        d.mkdir()
        (d / "edge-server.conf").write_text("[Service]\nRestart=always\n")
        sh(box, "install_md_alert", MD_ALERT="false")
        assert (d / "edge-server.conf").exists()


class TestConfigBackupScript:
    SCRIPT = REPO / "host" / "config-backup.sh"

    def run(self, box, dest, keep=14, **env):
        full = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
            "EDGE_SERVER_DIR": str(box / "app"),
            "SYSTEM_PATHS": str(box / "fstab") + " " + str(box / "missing-*"),
        }
        full.update({k: str(v) for k, v in env.items()})
        return subprocess.run(
            [BASH, str(self.SCRIPT), str(dest), str(keep)],
            capture_output=True,
            text=True,
            env=full,
            timeout=60,
        )

    @pytest.fixture
    def seeded(self, box):
        app = box / "app"
        (app / "frigate").mkdir()
        (app / ".cam").write_text("10.0.0.1 user:pass\n")
        (app / "docker-compose.yml").write_text("services: {}\n")
        (app / "frigate" / "config.yml").write_text("cameras: {}\n")
        (box / "fstab").write_text("# fstab\n")
        return box

    def test_archive_has_the_config_and_is_private(self, seeded):
        dest = seeded / "dest"
        r = self.run(seeded, dest)
        assert r.returncode == 0, r.stderr
        archives = list(dest.glob("config-*.tar.gz"))
        assert len(archives) == 1
        assert stat.S_IMODE(archives[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(dest.stat().st_mode) == 0o700
        with tarfile.open(archives[0]) as tar:
            names = [n.lstrip("/") for n in tar.getnames()]
        wanted = [
            str(seeded / p).lstrip("/") for p in ("app/.cam", "app/frigate/config.yml", "fstab")
        ]
        assert all(w in names for w in wanted), names

    def test_keeps_only_the_newest(self, seeded):
        dest = seeded / "dest"
        dest.mkdir()
        for i in range(4):  # old archives, named like ours
            f = dest / f"config-{os.uname().nodename.split('.')[0]}-2020010{i}-000000.tar.gz"
            f.write_text("old")
            os.utime(f, (1_600_000_000 + i, 1_600_000_000 + i))
        assert self.run(seeded, dest, keep=2).returncode == 0
        left = sorted(p.name for p in dest.glob("config-*.tar.gz"))
        assert len(left) == 2 and not any("20200100" in n for n in left)

    def test_unmounted_target_writes_nothing(self, seeded):
        if shutil.which("mountpoint") is None:
            pytest.skip("mountpoint not available")
        dest = seeded / "dest"
        r = self.run(seeded, dest, BACKUP_MOUNT=seeded / "not-a-mount")
        assert r.returncode == 1 and "not mounted" in r.stderr
        assert not dest.exists()

    def test_bad_arguments(self, seeded):
        assert self.run(seeded, "relative", 3).returncode == 1
        assert self.run(seeded, seeded / "d", "zero").returncode == 1

    def test_nothing_to_back_up_is_an_error(self, box):
        r = self.run(box, box / "dest", SYSTEM_PATHS=str(box / "nope"))
        assert r.returncode == 1 and "nothing to back up" in r.stderr


class TestMdAlertScript:
    SCRIPT = REPO / "host" / "md-alert.sh"

    def test_writes_the_event_to_the_log_file(self, tmp_path):
        out = tmp_path / "md.log"
        for args in (["Fail", "/dev/md0", "/dev/sdb"], ["DegradedArray", "/dev/md0"]):
            r = subprocess.run(
                [BASH, str(self.SCRIPT), *args],
                capture_output=True,
                text=True,
                env={"PATH": os.environ["PATH"], "MD_ALERT_LOG": str(out)},
                timeout=30,
            )
            assert r.returncode == 0, r.stderr
        text = out.read_text()
        assert "mdadm event Fail on /dev/md0 (/dev/sdb)" in text
        assert "mdadm event DegradedArray on /dev/md0" in text
