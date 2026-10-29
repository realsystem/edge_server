"""SSHClient tests that execute real (local) processes.

``_ssh_args`` is replaced with ``sh -c`` so the "remote" command runs locally,
which exercises the real subprocess plumbing: stdin handling, pipe draining,
timeouts and shell quoting.
"""

import shlex
from unittest.mock import patch

import pytest

from ssh import SSHClient, SSHResult


@pytest.fixture
def local():
    client = SSHClient(host="localhost", user="me", timeout=5, retries=3)
    with patch.object(SSHClient, "_ssh_args", return_value=["sh", "-c"]):
        yield client


def test_run_passes_input_on_stdin(local):
    result = local.run('read a; read b; echo "$b-$a"', input="one\ntwo\n")
    assert result.success
    assert result.stdout == "two-one"


def test_run_without_input_does_not_read_terminal(local):
    # stdin is /dev/null: `cat` must return immediately with no output
    result = local.run("cat", timeout=5)
    assert result.success
    assert result.stdout == ""


def test_run_timeout(local):
    result = local.run("exec sleep 5", timeout=1)
    assert not result.success
    assert "timed out" in result.stderr


def test_run_with_progress_large_output_does_not_deadlock(local):
    # ~1 MiB of output: more than a pipe buffer. The old poll()-based loop
    # blocked here until the timeout.
    calls = []
    result = local.run_with_progress(
        "head -c 1048576 /dev/zero | tr '\\0' x; echo; echo done >&2",
        timeout=20,
        on_progress=calls.append,
    )
    assert result.success, result.stderr
    assert len(result.stdout) == 1048576
    assert result.stderr == "done"
    assert calls  # progress reported at least once


def test_run_with_progress_timeout(local):
    result = local.run_with_progress("echo started; exec sleep 10", timeout=1)
    assert not result.success
    assert "timed out" in result.stderr


def test_run_with_progress_spawn_failure():
    client = SSHClient(host="h", user="u")
    with patch.object(SSHClient, "_ssh_args", return_value=["/nonexistent/binary"]):
        result = client.run_with_progress("true")
    assert result.returncode == -1


def test_run_script_quotes_env_values(local, tmp_path):
    script = tmp_path / "my script.sh"
    script.write_text('#!/bin/sh\nprintf "%s|%s" "$A" "$B"\n')
    script.chmod(0o755)
    marker = tmp_path / "pwned"
    evil = f"'; touch {marker}; echo '"
    result = local.run_script(str(script), env={"A": evil, "B": "x y"}, sudo=False)
    assert result.success, result.stdout
    assert result.stdout == f"{evil}|x y"
    assert not marker.exists()


def test_path_exists_and_mkdir_quote_paths(local, tmp_path):
    target = tmp_path / "dir with space; touch pwned"
    assert local.path_exists(str(target)) is False
    assert local.mkdir(str(target)) is True
    assert target.is_dir()
    assert local.path_exists(str(target)) is True
    assert not (tmp_path / "pwned").exists()


def test_get_docker_containers_parses_output():
    client = SSHClient(host="h", user="u")
    out = "mosquitto|Up 2 hours|0.0.0.0:1883->1883/tcp\nfrigate|Up 1 hour|\n"
    with patch.object(client, "run", return_value=SSHResult(0, out, "")) as run:
        containers = client.get_docker_containers("fri'gate")
    assert containers[0] == {
        "name": "mosquitto",
        "status": "Up 2 hours",
        "ports": "0.0.0.0:1883->1883/tcp",
    }
    assert len(containers) == 2
    cmd = run.call_args[0][0]
    assert shlex.quote("fri'gate") in cmd


def test_get_docker_containers_failure():
    client = SSHClient(host="h", user="u")
    with patch.object(client, "run", return_value=SSHResult(1, "", "boom")):
        assert client.get_docker_containers() == []


class TestRetries:
    def test_test_connection_retries_then_succeeds(self):
        client = SSHClient(host="h", user="u", retries=3)
        results = [SSHResult(255, "", ""), SSHResult(255, "", ""), SSHResult(0, "", "")]
        with patch.object(client, "run", side_effect=results) as run, patch("ssh.time.sleep"):
            assert client.test_connection() is True
        assert run.call_count == 3

    def test_test_connection_gives_up(self):
        client = SSHClient(host="h", user="u", retries=2)
        with (
            patch.object(client, "run", return_value=SSHResult(255, "", "")) as run,
            patch("ssh.time.sleep"),
        ):
            assert client.test_connection() is False
        assert run.call_count == 2

    def test_wait_for_host_zero_timeout(self):
        client = SSHClient(host="h", user="u")
        ok, elapsed = client.wait_for_host(timeout=0)
        assert ok is False and elapsed == 0.0

    def test_wait_for_host_success(self):
        client = SSHClient(host="h", user="u")
        with patch.object(client, "run", return_value=SSHResult(0, "", "")):
            assert client.wait_for_host(timeout=5)[0] is True


class TestMisc:
    def test_reboot_accepts_connection_closed(self):
        client = SSHClient(host="h", user="u")
        with patch.object(client, "run", return_value=SSHResult(255, "", "")):
            assert client.reboot() is True

    def test_os_info(self):
        client = SSHClient(host="h", user="u")
        with patch.object(client, "run", return_value=SSHResult(0, "Ubuntu 24.04 LTS", "")):
            assert client.get_os_info() == "Ubuntu 24.04 LTS"
        with patch.object(client, "run", return_value=SSHResult(1, "", "")):
            assert client.get_os_info() is None

    def test_copy_files_builds_scp_command(self, tmp_path):
        client = SSHClient(host="h", user="u", port=2222, key_file="/k")
        with patch("ssh.subprocess.run") as run:
            run.return_value.returncode = 0
            assert client.copy_files([tmp_path / "a.sh", tmp_path / "plugins"], "/var/tmp/x")
        argv = run.call_args[0][0]
        assert argv[0] == "scp" and "-r" in argv
        assert argv[argv.index("-P") + 1] == "2222"
        assert argv[-1] == "u@h:/var/tmp/x/"

    def test_copy_file_failure(self, tmp_path):
        client = SSHClient(host="h", user="u")
        with patch("ssh.subprocess.run", side_effect=OSError("no scp")):
            assert client.copy_file(tmp_path / "a", "/x") is False
