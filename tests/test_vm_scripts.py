"""ops/vm/setup-github-key.sh: the offline parts (key, ssh config, input handling). Linux only, like the VM it is for."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ops" / "vm" / "setup-github-key.sh"
BASH = shutil.which("bash")
LINUX = sys.platform.startswith("linux") and BASH and shutil.which("ssh-keygen")


def run(home, *args, cwd=None):
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [BASH, str(SCRIPT), "--offline", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=60,
    )


class ScriptText(unittest.TestCase):
    def test_it_uses_strict_mode_and_lf_endings(self):
        text = SCRIPT.read_bytes()
        self.assertTrue(text.startswith(b"#!/usr/bin/env bash"))
        self.assertIn(b"set -euo pipefail", text)
        self.assertNotIn(b"\r", text)

    def test_it_checks_github_against_a_published_fingerprint_and_never_disables_host_checking(
        self,
    ):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU", text)
        self.assertNotIn("StrictHostKeyChecking=no", text)
        self.assertNotIn("StrictHostKeyChecking no", text)

    def test_nothing_secret_is_in_the_file(self):
        self.assertNotIn("PRIVATE KEY", SCRIPT.read_text(encoding="utf-8"))

    @unittest.skipIf(BASH is None, "bash not installed")
    def test_it_parses(self):
        self.assertEqual(
            subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True).returncode, 0
        )


@unittest.skipUnless(LINUX, "needs a Linux shell with ssh-keygen")
class Offline(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_it_makes_a_private_key_a_config_block_and_prints_only_the_public_key(self):
        done = run(self.home, "acme/webapp")
        self.assertEqual(done.returncode, 0, done.stderr)
        key = self.home / ".ssh" / "deploy_webapp"
        self.assertTrue(key.exists() and (self.home / ".ssh" / "deploy_webapp.pub").exists())
        self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((self.home / ".ssh").stat().st_mode & 0o777), "0o700")
        config = (self.home / ".ssh" / "config").read_text()
        self.assertIn("Host github-webapp", config)
        self.assertIn("IdentitiesOnly yes", config)
        self.assertIn(f"IdentityFile {key}", config)
        self.assertIn("ssh-ed25519 ", done.stdout)
        self.assertNotIn("PRIVATE KEY", done.stdout + done.stderr)
        self.assertIn("https://github.com/acme/webapp/settings/keys/new", done.stdout)

    def test_running_again_changes_nothing(self):
        run(self.home, "acme/webapp")
        before = (
            (self.home / ".ssh" / "deploy_webapp").read_bytes(),
            (self.home / ".ssh" / "config").read_text(),
        )
        again = run(self.home, "acme/webapp")
        self.assertEqual(again.returncode, 0)
        after = (
            (self.home / ".ssh" / "deploy_webapp").read_bytes(),
            (self.home / ".ssh" / "config").read_text(),
        )
        self.assertEqual(before, after)
        self.assertEqual(after[1].count("Host github-webapp"), 1)

    def test_each_repo_gets_its_own_key_and_alias(self):
        run(self.home, "acme/webapp")
        run(self.home, "acme/Edge_Server.git")
        config = (self.home / ".ssh" / "config").read_text()
        self.assertIn("Host github-webapp", config)
        self.assertIn("Host github-edge-server", config)  # lower case, no underscore, no .git
        self.assertNotEqual(
            (self.home / ".ssh" / "deploy_webapp").read_bytes(),
            (self.home / ".ssh" / "deploy_edge-server").read_bytes(),
        )

    def test_the_repo_is_read_from_the_origin_of_the_clone_it_runs_in(self):
        clone = self.home / "clone"
        clone.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/acme/edge_server.git"],
            cwd=clone,
            check=True,
        )
        done = run(self.home, cwd=clone)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("github.com/acme/edge_server/settings/keys/new", done.stdout)

    def test_bad_input_is_refused_before_anything_is_created(self):
        for args in (("not a repo",), ("owner/repo; rm -rf /",), ("--bogus",), ("a/b", "c/d")):
            self.assertNotEqual(run(self.home, *args).returncode, 0, args)
        self.assertFalse((self.home / ".ssh").exists())


if __name__ == "__main__":
    unittest.main()
