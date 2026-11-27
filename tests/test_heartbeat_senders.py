"""ops/heartbeat: the host sender and its installer (run with stub curl and df), and the generic Home Assistant package."""

import json
import os
import shutil
import stat
import subprocess
import urllib.parse
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
HB = ROOT / "ops" / "heartbeat"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")

KUMA = "https://monitor.example.com/api/push/SECRETTOKEN123"
DF_95 = (
    '#!/usr/bin/env bash\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
    '/dev/x 100 95 5 95%% /\\n"\n'
)
DF_40 = DF_95.replace("95", "40").replace("100 40", "100 40")


def posix(path):
    """The path as bash sees it (on Windows with Git Bash a drive path becomes /c/...; on Linux unchanged)."""
    text = str(path)
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


@pytest.fixture
def box(tmp_path):
    """A scratch machine: stub curl (records its arguments), stub df, and a settings file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "curl-args"
    for name, body in {
        "curl": '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$STUB_OUT"\nexit "${STUB_EXIT:-0}"\n',
        "df": DF_40,
    }.items():
        f = bin_dir / name
        f.write_text(body, encoding="utf-8", newline="\n")
        f.chmod(0o755)
    return {"dir": tmp_path, "bin": bin_dir, "out": out, "conf": tmp_path / "hb.env"}


def run_sender(box, settings, *args, extra_env=None, df=None):
    if df is not None:
        (box["bin"] / "df").write_text(df, encoding="utf-8", newline="\n")
    box["conf"].write_text(
        "".join(f"{k}={v}\n" for k, v in settings.items()), encoding="utf-8", newline="\n"
    )
    env = {
        **os.environ,
        "PATH": posix(box["bin"]) + os.pathsep + os.environ["PATH"],
        "STUB_OUT": posix(box["out"]),
        "EDGE_HEARTBEAT_CONF": posix(box["conf"]),
        **(extra_env or {}),
    }
    for key in (
        "HEARTBEAT_URL",
        "HEARTBEAT_NAME",
        "HEARTBEAT_DEGRADED_AS",
        "HEARTBEAT_DISK_DEGRADED_PERCENT",
    ):
        if key not in (extra_env or {}):
            env.pop(key, None)
    return subprocess.run(
        [BASH, str(HB / "node-heartbeat.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def curl_args(box):
    return box["out"].read_text(encoding="utf-8").splitlines() if box["out"].exists() else None


@needs_bash
class TestHostSender:
    def test_a_kuma_push_url_gets_status_and_message_and_loses_its_old_query(self, box):
        done = run_sender(
            box, {"HEARTBEAT_URL": KUMA + "?status=up&msg=OK&ping=", "HEARTBEAT_NAME": "nas-1"}
        )
        assert done.returncode == 0, done.stderr
        args = curl_args(box)
        assert args[args.index("-X") + 1] == "POST"
        url = args[-1]
        assert url.startswith(KUMA + "?status=up&msg=ok%20%7C%20up%20")
        assert "ping=" not in url and "msg=OK" not in url  # the pasted query is ignored
        body = json.loads(args[args.index("--data") + 1])
        assert body["host"] == "nas-1" and body["status"] == "ok"

    def test_any_other_url_is_called_exactly_as_stored(self, box):
        assert run_sender(box, {"HEARTBEAT_URL": "https://hc.example.com/abc?x=1"}).returncode == 0
        assert curl_args(box)[-1] == "https://hc.example.com/abc?x=1"

    @pytest.mark.parametrize("stored", ["", "unknown", "ftp://example.com/x", "not a url"])
    def test_nothing_is_sent_without_a_real_address(self, box, stored):
        done = run_sender(box, {"HEARTBEAT_URL": stored})
        assert done.returncode == 0 and "nothing sent" in done.stdout
        assert curl_args(box) is None

    def test_a_full_disk_degrades_the_message_but_only_alerts_when_asked(self, box):
        done = run_sender(box, {"HEARTBEAT_URL": KUMA}, df=DF_95)
        assert (
            "degraded" in done.stdout and "status=up" in curl_args(box)[-1]
        )  # default: the message only
        run_sender(box, {"HEARTBEAT_URL": KUMA, "HEARTBEAT_DEGRADED_AS": "down"}, df=DF_95)
        assert "status=down" in curl_args(box)[-1]
        run_sender(box, {"HEARTBEAT_URL": KUMA, "HEARTBEAT_DEGRADED_AS": "down"}, df=DF_40)
        assert "status=up" in curl_args(box)[-1]  # a healthy host stays up

    def test_the_limit_and_the_watched_disk_can_be_set(self, box):
        run_sender(box, {"HEARTBEAT_URL": KUMA, "HEARTBEAT_DISK_DEGRADED_PERCENT": "99"}, df=DF_95)
        assert "ok%20%7C" in curl_args(box)[-1]  # 95 is below a limit of 99

    def test_the_message_decodes_to_the_summary_it_prints(self, box):
        done = run_sender(box, {"HEARTBEAT_URL": KUMA}, "--dry-run")
        summary = next(line for line in done.stdout.splitlines() if line.startswith("summary: "))[
            len("summary: ") :
        ]
        shown = next(
            line for line in done.stdout.splitlines() if line.startswith("would POST to: ")
        )
        assert urllib.parse.unquote(shown.split("msg=", 1)[1]) == summary

    def test_a_dry_run_sends_nothing_and_hides_the_token(self, box):
        done = run_sender(box, {"HEARTBEAT_URL": KUMA}, "--dry-run")
        assert done.returncode == 0 and curl_args(box) is None
        assert (
            "SECRETTOKEN123" not in done.stdout + done.stderr
            and "/api/push/<hidden>" in done.stdout
        )

    def test_the_environment_wins_over_the_file(self, box):
        run_sender(
            box,
            {"HEARTBEAT_URL": "https://file.example.com/x"},
            extra_env={"HEARTBEAT_URL": "https://env.example.com/y"},
        )
        assert curl_args(box)[-1] == "https://env.example.com/y"

    def test_bad_settings_are_refused(self, box):
        assert (
            run_sender(box, {"HEARTBEAT_URL": KUMA, "HEARTBEAT_DEGRADED_AS": "sideways"}).returncode
            == 2
        )
        assert (
            run_sender(
                box, {"HEARTBEAT_URL": KUMA, "HEARTBEAT_DISK_DEGRADED_PERCENT": "lots"}
            ).returncode
            == 2
        )
        assert run_sender(box, {"HEARTBEAT_URL": KUMA}, "--bogus").returncode == 2

    def test_a_failed_send_is_a_failed_run(self, box):
        done = run_sender(box, {"HEARTBEAT_URL": KUMA}, extra_env={"STUB_EXIT": "22"})
        assert done.returncode != 0  # systemd shows it as failed; the timer keeps going


def run_install(box, *args, stdin=None, env=None):
    return subprocess.run(
        [BASH, str(HB / "install.sh"), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        input=stdin,
        timeout=60,
    )


@needs_bash
class TestInstaller:
    def installed(self, box, *args, url=KUMA):
        root = box["dir"] / "root"
        url_file = box["dir"] / "url"
        url_file.write_text(url + "\n", encoding="utf-8")
        done = run_install(box, "--root", posix(root), "--url-file", posix(url_file), *args)
        return root, done

    def test_it_installs_the_script_the_settings_and_the_timer(self, box):
        root, done = self.installed(box, "--name", "nas-1")
        assert done.returncode == 0, done.stderr
        assert (root / "usr/local/sbin/edge-heartbeat").read_bytes() == (
            HB / "node-heartbeat.sh"
        ).read_bytes()
        conf = (root / "etc/edge-heartbeat.env").read_text()
        assert f"HEARTBEAT_URL={KUMA}\n" in conf and "HEARTBEAT_NAME=nas-1\n" in conf
        timer = (root / "etc/systemd/system/edge-heartbeat.timer").read_text()
        assert "OnUnitActiveSec=5min" in timer and "WantedBy=timers.target" in timer
        assert (
            "ExecStart=/usr/local/sbin/edge-heartbeat"
            in (root / "etc/systemd/system/edge-heartbeat.service").read_text()
        )
        if os.name != "nt":
            assert stat.S_IMODE((root / "etc/edge-heartbeat.env").stat().st_mode) == 0o600

    def test_the_url_is_never_printed(self, box):
        _, done = self.installed(box)
        assert "SECRETTOKEN123" not in done.stdout + done.stderr

    def test_the_interval_and_modes_are_applied(self, box):
        root, done = self.installed(
            box, "--interval-min", "10", "--degraded-as", "down", "--disk-percent", "80"
        )
        assert done.returncode == 0
        assert (
            "OnUnitActiveSec=10min"
            in (root / "etc/systemd/system/edge-heartbeat.timer").read_text()
        )
        conf = (root / "etc/edge-heartbeat.env").read_text()
        assert "HEARTBEAT_DEGRADED_AS=down" in conf and "HEARTBEAT_DISK_DEGRADED_PERCENT=80" in conf

    def test_running_it_again_replaces_the_files_cleanly(self, box):
        root, _ = self.installed(box)
        root, done = self.installed(box, url="https://monitor.example.com/api/push/ANOTHERTOKEN")
        assert done.returncode == 0
        assert (root / "etc/edge-heartbeat.env").read_text().count("HEARTBEAT_URL=") == 1
        assert "ANOTHERTOKEN" in (root / "etc/edge-heartbeat.env").read_text()

    def test_the_installed_sender_works_with_the_installed_settings(self, box):
        root, _ = self.installed(box, "--name", "nas-1")
        env = {
            **os.environ,
            "EDGE_HEARTBEAT_CONF": posix(root / "etc/edge-heartbeat.env"),
            "PATH": posix(box["bin"]) + os.pathsep + os.environ["PATH"],
            "STUB_OUT": posix(box["out"]),
        }
        done = subprocess.run(
            [BASH, str(root / "usr/local/sbin/edge-heartbeat"), "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert done.returncode == 0 and '"host":"nas-1"' in done.stdout

    @pytest.mark.parametrize(
        "args",
        [
            ["--interval-min", "0"],
            ["--interval-min", "61"],
            ["--interval-min", "soon"],
            ["--degraded-as", "sideways"],
            ["--disk-percent", "0"],
            ["--name", "bad;name"],
            ["--bogus"],
        ],
    )
    def test_bad_options_are_refused_before_anything_is_written(self, box, args):
        root = box["dir"] / "root"
        url_file = box["dir"] / "url"
        url_file.write_text(KUMA + "\n")
        done = run_install(box, "--root", posix(root), "--url-file", posix(url_file), *args)
        assert done.returncode != 0 and not root.exists()

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/x",
            "monitor.example.com/api/push/x",
            "https://x.example.com/a b",
            "https://x.example.com/a'b",
        ],
    )
    def test_a_url_that_is_not_a_plain_address_is_refused(self, box, url):
        root, done = self.installed(box, url=url)
        assert done.returncode != 0 and not (root / "etc/edge-heartbeat.env").exists()

    def test_without_a_url_and_without_a_terminal_it_says_what_to_do(self, box):
        done = run_install(
            box, "--root", posix(box["dir"] / "root"), env={"HEARTBEAT_URL": ""}, stdin=""
        )
        assert done.returncode != 0 and "--url-file" in done.stderr

    def test_uninstall_removes_everything_it_made(self, box):
        root, _ = self.installed(box)
        done = run_install(box, "--root", posix(root), "--uninstall")
        assert done.returncode == 0
        for rel in (
            "usr/local/sbin/edge-heartbeat",
            "etc/edge-heartbeat.env",
            "etc/systemd/system/edge-heartbeat.timer",
        ):
            assert not (root / rel).exists()

    def test_the_scripts_are_strict_and_use_unix_line_endings(self):
        for name in ("install.sh", "node-heartbeat.sh"):
            data = (HB / name).read_bytes()
            assert (
                data.startswith(b"#!/usr/bin/env bash")
                and b"set -euo pipefail" in data
                and b"\r" not in data
            )
            assert subprocess.run([BASH, "-n", str(HB / name)], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load((HB / "ha" / "edge_heartbeat.yaml").read_text(encoding="utf-8"))


class TestHomeAssistantPackage:
    def test_the_url_helper_is_hidden_and_has_no_value_in_the_file(self, doc):
        helper = doc["input_text"]["edge_heartbeat_url"]
        assert helper["mode"] == "password" and "initial" not in helper

    def test_the_names_cannot_clash_with_other_packages(self, doc):
        names = (
            list(doc["input_text"])
            + list(doc["input_datetime"])
            + list(doc["rest_command"])
            + [a["id"] for a in doc["automation"]]
        )
        assert all(n.startswith("edge_") for n in names)

    def test_it_sends_every_five_minutes(self, doc):
        assert doc["automation"][0]["trigger"][0]["minutes"] == "/5"

    def render(self, template, states=None, attrs=None):
        jinja2 = pytest.importorskip("jinja2")
        from datetime import datetime, timezone

        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        states = states or {}
        attrs = attrs or {}

        class Entity:
            def __init__(self, entity_id, state):
                self.entity_id, self.state = entity_id, state

        class States:
            def __call__(self, entity_id):
                return states.get(entity_id, "unknown")

            def __iter__(self):
                return iter(Entity(k, v) for k, v in states.items())

        def as_timestamp(value, default=None):
            try:
                return datetime.fromisoformat(str(value)).timestamp()
            except ValueError:
                return default

        env = jinja2.Environment()
        env.globals.update(
            states=States(),
            state_attr=lambda e, a: attrs.get((e, a)),
            now=lambda: now,
            as_timestamp=as_timestamp,
        )
        return " ".join(env.from_string(template).render().split())

    def test_nothing_is_sent_until_the_helper_holds_a_real_address(self, doc):
        cond = doc["automation"][0]["condition"][0]["value_template"]
        for unset in ("", "unknown", "unavailable", "none"):
            assert self.render(cond, {"input_text.edge_heartbeat_url": unset}) == "False", unset
        assert (
            self.render(
                cond, {"input_text.edge_heartbeat_url": "https://monitor.example.com/api/push/x"}
            )
            == "True"
        )

    def test_the_summary_and_payload_report_uptime_and_unavailable_entities(self, doc):
        sensor = next(
            s
            for block in doc["template"]
            for s in block.get("sensor", [])
            if s["unique_id"] == "edge_heartbeat"
        )
        states = {
            "sensor.edge_ha_started": "2026-07-06T09:30:00+00:00",
            "light.a": "unavailable",
            "light.b": "on",
            "sensor.c": "unavailable",
        }
        assert (
            self.render(sensor["attributes"]["summary"], states)
            == "ok | HA up 2.5 h | 2 unavailable"
        )
        payload = json.loads(
            self.render(
                sensor["attributes"]["payload"], states, {("zone.home", "friendly_name"): "Home"}
            )
        )
        assert payload == {
            "site": "Home",
            "status": "ok",
            "ha_uptime_hours": 2.5,
            "unavailable_entities": 2,
        }

    def test_a_kuma_url_gets_status_and_message_and_any_other_url_is_untouched(self, doc):
        url_template = doc["rest_command"]["edge_heartbeat"]["url"]
        attrs = {("sensor.edge_heartbeat", "summary"): "ok | HA up 2.5 h | 2 unavailable"}
        kuma = "https://monitor.example.com/api/push/TOKEN123"
        got = self.render(
            url_template, {"input_text.edge_heartbeat_url": kuma + "?status=up&msg=OK&ping="}, attrs
        )
        assert got == kuma + "?status=up&msg=ok%20%7C%20HA%20up%202.5%20h%20%7C%202%20unavailable"
        plain = "https://hc.example.com/abc?x=1"
        assert self.render(url_template, {"input_text.edge_heartbeat_url": plain}, attrs) == plain
