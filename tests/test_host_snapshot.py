"""telegram-bot/host-snapshot.sh: firewall, ports and service state for /diag."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "telegram-bot" / "host-snapshot.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


@pytest.fixture
def fake_tools(tmp_path):
    """ufw, ss and iptables that print known text."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    outputs = {
        "ufw": "Status: active\n22/tcp ALLOW IN 192.168.0.0/16",
        "ss": 'tcp LISTEN 0 4096 0.0.0.0:5000 users:(("frigate"))',
        "iptables": "-P FORWARD DROP\n-A DOCKER-USER -j RETURN",
    }
    for name, text in outputs.items():
        tool = bin_dir / name
        tool.write_text(f"#!/usr/bin/env bash\ncat <<'EOT'\n{text}\nEOT\n")
        tool.chmod(0o755)
    return bin_dir


def run(args, bin_dir=None):
    env = {**os.environ}
    if bin_dir:
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=120
    )


def test_prints_firewall_ports_and_services(fake_tools):
    result = run([], fake_tools)
    assert result.returncode == 0, result.stderr
    text = result.stdout
    for heading in (
        "Listening ports",
        "Firewall: UFW",
        "Firewall: iptables filter table",
        "Firewall: iptables NAT table",
        "Firewall: IPv6",
        "Docker: published ports",
        "Network interfaces and routes",
        "SSH daemon",
        "Services and timers",
        "Recent system errors",
    ):
        assert f"--- {heading}" in text, heading
    assert "22/tcp ALLOW IN 192.168.0.0/16" in text  # ufw output
    assert '0.0.0.0:5000 users:(("frigate"))' in text  # ss output
    assert "-A DOCKER-USER -j RETURN" in text  # iptables output
    assert "$ ufw status verbose" in text  # the command is shown with its output


def test_missing_tools_are_reported_not_fatal(tmp_path):
    # a PATH with only the basics: nothing like ufw or docker
    basics = tmp_path / "basics"
    basics.mkdir()
    for name in (
        "bash",
        "date",
        "hostname",
        "head",
        "grep",
        "cat",
        "tr",
        "mktemp",
        "chmod",
        "mv",
        "timeout",
    ):
        found = shutil.which(name)
        if found:
            (basics / name).symlink_to(found)
    result = subprocess.run(
        [str(basics / "bash"), str(SCRIPT)],
        env={"PATH": str(basics)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "(not installed: ufw)" in result.stdout
    assert "(not installed: docker)" in result.stdout


def test_writes_a_world_readable_file_atomically(fake_tools, tmp_path):
    out = tmp_path / "status" / "host-snapshot.txt"
    out.parent.mkdir()
    result = run([str(out)], fake_tools)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o644  # the bot's container reads it
    assert "Host snapshot" in out.read_text()
    assert [p.name for p in out.parent.iterdir()] == ["host-snapshot.txt"]  # no temp file left


def test_script_is_executable_and_valid():
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_script_only_reads():
    """The snapshot must never change the system."""
    text = SCRIPT.read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    for word in (
        "iptables -A",
        "iptables -I",
        "iptables -D",
        "iptables -F",
        "ufw enable",
        "ufw allow",
        "ufw delete",
        "systemctl restart",
        "systemctl stop",
        "rm -",
    ):
        assert word not in code, word
