"""telegram-bot/smart-report.py: the host-side disk health summary."""

import importlib.util
import json
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "smart_report", REPO / "telegram-bot" / "smart-report.py"
)
smart = importlib.util.module_from_spec(spec)
sys.modules["smart_report"] = smart
spec.loader.exec_module(smart)

NVME = {
    "device": {"name": "/dev/nvme0", "type": "nvme"},
    "model_name": "WD Blue SN570",
    "smart_status": {"passed": True},
    "temperature": {"current": 41},
    "power_on_time": {"hours": 1234},
    "nvme_smart_health_information_log": {
        "percentage_used": 7,
        "media_errors": 0,
        "critical_warning": 0,
    },
}


def ata(attributes, passed=True):
    return {
        "device": {"name": "/dev/sda", "type": "sat"},
        "model_name": "Samsung SSD 860",
        "smart_status": {"passed": passed},
        "ata_smart_attributes": {"table": attributes},
    }


def attr(ident, value=100, raw=0):
    return {"id": ident, "value": value, "raw": {"value": raw}}


class TestParse:
    def test_nvme(self):
        disk = smart.parse_disk(NVME)
        assert disk["device"] == "nvme0" and disk["model"] == "WD Blue SN570"
        assert disk["passed"] is True and disk["wear_used"] == 7
        assert disk["temperature"] == 41 and disk["hours"] == 1234

    def test_nvme_critical_warning_fails_the_disk(self):
        data = json.loads(json.dumps(NVME))
        data["nvme_smart_health_information_log"]["critical_warning"] = 4
        assert smart.parse_disk(data)["passed"] is False

    def test_sata_wear_is_the_normalized_value_counting_down(self):
        disk = smart.parse_disk(ata([attr(177, value=88)]))
        assert disk["wear_used"] == 12
        assert smart.parse_disk(ata([attr(231, value=60)]))["wear_used"] == 40

    def test_bad_sectors(self):
        disk = smart.parse_disk(ata([attr(5, raw=8), attr(197, raw=2), attr(198, raw=5)]))
        assert disk["reallocated"] == 8 and disk["pending"] == 5

    def test_smart_failure(self):
        assert smart.parse_disk(ata([], passed=False))["passed"] is False

    def test_drive_without_smart_is_skipped(self):
        assert (
            smart.parse_disk(
                {"device": {"name": "/dev/sdb"}, "smart_support": {"available": False}}
            )
            is None
        )

    def test_odd_values_do_not_crash(self):
        assert smart.parse_disk(ata([attr(177, value=250)]))["wear_used"] is None
        assert smart.parse_disk({})["device"] == "?"


class TestCollect:
    def runner(self, replies):
        def run(args):
            return replies.get(tuple(args))

        return run

    def test_collects_every_scanned_disk(self):
        replies = {
            ("--scan",): {
                "devices": [
                    {"name": "/dev/sda", "type": "sat"},
                    {"name": "/dev/nvme0", "type": "nvme"},
                ]
            },
            ("-a", "/dev/sda", "-d", "sat"): ata([attr(177, value=95)]),
            ("-a", "/dev/nvme0", "-d", "nvme"): NVME,
        }
        report = smart.collect(self.runner(replies))
        assert [d["device"] for d in report["disks"]] == ["sda", "nvme0"]
        assert "error" not in report and report["generated"] > 0

    def test_a_disk_that_does_not_answer_is_left_out(self):
        replies = {("--scan",): {"devices": [{"name": "/dev/sda", "type": "sat"}]}}
        assert smart.collect(self.runner(replies))["disks"] == []

    def test_missing_smartctl_is_reported(self):
        report = smart.collect(lambda args: None)
        assert report["disks"] == [] and "smartctl" in report["error"]


class TestMain:
    def fake_smartctl(self, tmp_path, monkeypatch):
        binary = tmp_path / "bin"
        binary.mkdir()
        script = binary / "smartctl"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$2" == "--scan" ]]; then\n'
            '  echo \'{"devices": [{"name": "/dev/sda", "type": "sat"}]}\'\n'
            "else\n"
            f"  echo '{json.dumps(ata([attr(5, raw=3)]))}'\n"
            "  exit 4\n"  # smartctl's exit status is a bit mask of findings
            "fi\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{binary}:/usr/bin:/bin")

    def test_writes_the_report_atomically_and_world_readable(self, tmp_path, monkeypatch, capsys):
        self.fake_smartctl(tmp_path, monkeypatch)
        out = tmp_path / "status" / "disks.json"
        assert smart.main([str(out)]) == 0
        report = json.loads(out.read_text())
        assert report["disks"][0]["reallocated"] == 3
        assert stat.S_IMODE(out.stat().st_mode) == 0o644
        assert not list(out.parent.glob("*.tmp"))
        assert "1 disk" in capsys.readouterr().out

    def test_without_smartctl_it_writes_the_error_and_fails(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("PATH", str(tmp_path))
        out = tmp_path / "disks.json"
        assert smart.main([str(out)]) == 1
        assert "smartctl" in json.loads(out.read_text())["error"]
        assert "smartctl" in capsys.readouterr().err
