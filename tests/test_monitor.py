"""The off-site monitor deployment (ops/monitor): what can be checked without a VM."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MON = ROOT / "ops" / "monitor"
BASH = shutil.which("bash")


def posix(path):
    """The path as bash sees it (on Windows with Git Bash a drive path becomes /c/...; on Linux unchanged)."""
    text = str(path)
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


def caddyfile(*args):
    out = subprocess.run(
        [BASH, str(MON / "deploy.sh"), "--print-caddyfile", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.returncode, out.stdout, out.stderr


class Compose(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load((MON / "docker-compose.yml").read_text(encoding="utf-8"))[
            "services"
        ]

    def test_kuma_is_published_on_loopback_only_and_our_caddy_is_optional(self):
        self.assertEqual(
            self.doc["kuma"]["ports"], ["127.0.0.1:3001:3001"]
        )  # never on the internet
        self.assertEqual(sorted(self.doc["caddy"]["ports"]), ["443:443", "80:80"])
        self.assertEqual(
            self.doc["caddy"]["profiles"], ["docker-caddy"]
        )  # off unless no Caddy runs on the VM already

    def test_both_services_restart_and_the_data_is_on_disk(self):
        for name in ("kuma", "caddy"):
            self.assertEqual(self.doc[name]["restart"], "unless-stopped")
        self.assertIn("./data/kuma:/app/data", self.doc["kuma"]["volumes"])
        self.assertIn(
            "./data/caddy:/data", self.doc["caddy"]["volumes"]
        )  # certificates survive a restart

    def test_the_caddyfile_is_read_only_in_the_container(self):
        self.assertIn("./Caddyfile:/etc/caddy/Caddyfile:ro", self.doc["caddy"]["volumes"])


@unittest.skipIf(BASH is None, "bash not installed")
class DeployScript(unittest.TestCase):
    def test_the_script_parses_and_uses_strict_mode(self):
        text = (MON / "deploy.sh").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))
        self.assertIn("set -euo pipefail", text)
        self.assertNotIn("\r", text)  # CRLF would break it on Linux (.gitattributes keeps LF)
        done = subprocess.run([BASH, "-n", str(MON / "deploy.sh")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_script_refuses_to_run_as_a_normal_user(self):
        text = (MON / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn('[ "$(id -u)" -eq 0 ] || die', text)

    def test_the_caddyfile_without_restrictions_proxies_everything(self):
        code, out, _ = caddyfile("--domain", "monitor.example.com")
        self.assertEqual(code, 0)
        self.assertIn("monitor.example.com {", out)
        self.assertIn("reverse_proxy kuma:3001", out)
        self.assertNotIn("remote_ip", out)

    def test_admin_ips_leave_only_the_push_endpoint_public(self):
        code, out, _ = caddyfile(
            "--domain",
            "m.example.com",
            "--admin-ip",
            "203.0.113.7/32,198.51.100.0/24",
            "--email",
            "me@example.com",
        )
        self.assertEqual(code, 0)
        self.assertIn("not path /api/push/*", out)
        self.assertIn("not remote_ip 203.0.113.7/32 198.51.100.0/24", out)
        self.assertIn('respond @outside "Forbidden" 403', out)
        self.assertIn("email me@example.com", out)

    def test_bad_input_is_refused(self):
        self.assertNotEqual(caddyfile("--domain", "bad domain")[0], 0)
        self.assertNotEqual(caddyfile("--admin-ip", "not-an-ip")[0], 0)
        self.assertNotEqual(caddyfile("--email", "nope")[0], 0)
        self.assertNotEqual(caddyfile("--watchdog-url", "http://insecure.example.com/x")[0], 0)
        self.assertNotEqual(caddyfile("--domain", "x.example.com; rm -rf /")[0], 0)
        self.assertNotEqual(caddyfile("--caddy", "sideways")[0], 0)

    def test_the_printed_blocks_match_the_mode(self):
        host = caddyfile("--caddy", "host", "--domain", "m.example.com")[1]
        docker = caddyfile("--caddy", "docker", "--domain", "m.example.com")[1]
        none = caddyfile("--caddy", "none", "--domain", "m.example.com")[1]
        self.assertIn(
            "reverse_proxy 127.0.0.1:3001", host
        )  # an existing Caddy on the VM reaches Kuma on loopback
        self.assertIn("reverse_proxy kuma:3001", docker)  # our own Caddy reaches it by service name
        self.assertIn("reverse_proxy 127.0.0.1:3001", none)

    def test_the_next_steps_say_to_choose_sqlite_and_the_static_ip_hint_pins_the_current_address(
        self,
    ):
        text = (MON / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("choose SQLite", text)  # Kuma 2 asks for a database on first visit
        self.assertIn(
            "--addresses ${PUBLIC_IP", text
        )  # without it a NEW address would be reserved and the name would change

    def test_the_script_waits_for_kuma_before_it_touches_caddy_and_checks_https(self):
        text = (MON / "deploy.sh").read_text(encoding="utf-8")
        waits = text.index("Waiting for Uptime Kuma to start")
        self.assertIn("http://127.0.0.1:3001/", text[waits : waits + 600])
        self.assertLess(waits, text.rindex("apply_caddy_block"))  # the call, not the definition
        self.assertLess(waits, text.index("Waiting for HTTPS"))

    def test_no_secret_or_token_is_written_into_the_repository_files(self):
        for path in MON.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("hc-ping.com/" + "0", text.replace("<uuid>", ""))  # no real check URL
            self.assertNotRegex(text, r"(?i)(password|token)\s*=\s*['\"]?[A-Za-z0-9]{12,}")


STUBS = {
    "caddy": '#!/usr/bin/env bash\n[ "${STUB_CADDY_FAIL:-0}" = 1 ] && { echo "Error: unrecognized directive" >&2; exit 1; }\nexit 0\n',
    "systemctl": '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\n[ "${STUB_RELOAD_FAIL:-0}" = 1 ] && exit 1\nexit 0\n',
}
EXISTING = (
    "{\n\temail ops@example.com\n}\n\nexisting.example.com {\n\treverse_proxy 127.0.0.1:9000\n}\n"
)


@unittest.skipIf(BASH is None, "bash not installed")
class ExistingCaddy(unittest.TestCase):
    """--caddy-block-only runs just the step that edits an existing Caddy's Caddyfile, with stub caddy and systemctl."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "bin").mkdir()
        for name, body in STUBS.items():
            f = self.dir / "bin" / name
            f.write_text(body, encoding="utf-8", newline="\n")
            f.chmod(0o755)
        self.log = self.dir / "log"
        self.log.write_text("")
        self.caddyfile = self.dir / "Caddyfile"
        self.caddyfile.write_text(EXISTING, encoding="utf-8", newline="\n")

    def tearDown(self):
        self._tmp.cleanup()

    def run_block(
        self, *args, domain="monitor.1-2-3-4.sslip.io", fail_validate=False, fail_reload=False
    ):
        env = {
            **os.environ,
            "PATH": f"{posix(self.dir / 'bin')}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": posix(self.log),
        }
        if fail_validate:
            env["STUB_CADDY_FAIL"] = "1"
        if fail_reload:
            env["STUB_RELOAD_FAIL"] = "1"
        argv = [
            BASH,
            str(MON / "deploy.sh"),
            "--caddy-block-only",
            "--domain",
            domain,
            "--caddyfile",
            posix(self.caddyfile),
            *args,
        ]
        return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)

    def backups(self):
        return sorted(self.dir.glob("Caddyfile.edge-monitor-backup-*"))

    def test_the_block_is_appended_and_nothing_else_changes(self):
        done = self.run_block("--admin-ip", "203.0.113.7/32")
        self.assertEqual(done.returncode, 0, done.stderr)
        text = self.caddyfile.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(EXISTING))  # your sites and global options are untouched
        self.assertIn("# BEGIN edge-monitor", text)
        self.assertIn("monitor.1-2-3-4.sslip.io {", text)
        self.assertIn("reverse_proxy 127.0.0.1:3001", text)  # Kuma on loopback, not a second Caddy
        self.assertIn("not remote_ip 203.0.113.7/32", text)
        self.assertEqual(text.count("# END edge-monitor"), 1)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_text(encoding="utf-8"), EXISTING)
        self.assertIn("systemctl reload caddy", self.log.read_text())

    def test_running_again_refreshes_the_block_instead_of_adding_another(self):
        self.run_block("--admin-ip", "203.0.113.7/32")
        self.run_block("--admin-ip", "198.51.100.9/32")
        text = self.caddyfile.read_text(encoding="utf-8")
        self.assertEqual(text.count("# BEGIN edge-monitor"), 1)
        self.assertEqual(text.count("monitor.1-2-3-4.sslip.io {"), 1)
        self.assertIn("198.51.100.9/32", text)
        self.assertNotIn("203.0.113.7/32", text)
        self.assertTrue(text.startswith(EXISTING))

    def test_a_config_caddy_rejects_is_rolled_back(self):
        done = self.run_block(fail_validate=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"), EXISTING)
        self.assertNotIn("reload", self.log.read_text())  # never reloaded a bad config
        self.assertIn("restored", done.stderr)

    def test_a_failed_reload_restores_the_file(self):
        done = self.run_block(fail_reload=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"), EXISTING)

    def test_a_name_the_caddyfile_already_serves_is_refused_and_nothing_is_written(self):
        done = self.run_block(domain="existing.example.com")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"), EXISTING)
        self.assertEqual(self.backups(), [])
        self.assertIn("already has a site", done.stderr)

    def test_damaged_markers_stop_the_script_before_it_can_delete_anything(self):
        broken = (
            EXISTING + "\n# BEGIN edge-monitor (old)\nold.example.com {\n}\n"
        )  # no END marker: sed would eat to the end
        self.caddyfile.write_text(broken, encoding="utf-8", newline="\n")
        done = self.run_block()
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.caddyfile.read_text(encoding="utf-8"), broken)
        self.assertIn("damaged", done.stderr)


if __name__ == "__main__":
    unittest.main()
