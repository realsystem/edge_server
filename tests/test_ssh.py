"""Tests for SSH module."""

import subprocess
from unittest.mock import MagicMock, patch

from ssh import SSHClient, SSHResult


class TestSSHResult:
    """Tests for SSHResult dataclass."""

    def test_success_on_zero_returncode(self):
        """Test success property with returncode 0."""
        result = SSHResult(0, "output", "")
        assert result.success is True

    def test_failure_on_nonzero_returncode(self):
        """Test success property with non-zero returncode."""
        result = SSHResult(1, "", "error")
        assert result.success is False


class TestSSHClient:
    """Tests for SSHClient class."""

    def test_init_defaults(self):
        """Test SSHClient default values."""
        client = SSHClient(host="example.com", user="testuser")
        assert client.host == "example.com"
        assert client.user == "testuser"
        assert client.timeout == 10
        assert client.retries == 3
        assert client.key_file is None
        assert client.strict_host_key_checking == "accept-new"

    def test_init_custom_values(self):
        """Test SSHClient with custom values."""
        client = SSHClient(
            host="example.com",
            user="testuser",
            timeout=30,
            retries=5,
            key_file="/path/to/key",
            strict_host_key_checking="no",
        )
        assert client.timeout == 30
        assert client.retries == 5
        assert client.key_file == "/path/to/key"
        assert client.strict_host_key_checking == "no"

    def test_ssh_args_basic(self):
        """Test SSH arguments generation."""
        client = SSHClient(host="example.com", user="testuser", timeout=10)
        args = client._ssh_args()

        assert "ssh" in args
        assert "-o" in args
        assert "ConnectTimeout=10" in args
        assert "StrictHostKeyChecking=accept-new" in args
        assert "BatchMode=yes" in args
        assert "testuser@example.com" in args

    def test_ssh_args_with_key(self):
        """Test SSH arguments with key file."""
        client = SSHClient(host="example.com", user="testuser", key_file="/path/to/key")
        args = client._ssh_args()

        assert "-i" in args
        assert "/path/to/key" in args


class TestSSHRunWithProgress:
    """Tests for run_with_progress method."""

    @patch("subprocess.Popen")
    def test_calls_progress_callback(self, mock_popen):
        """Test that progress callback is called while the process runs."""
        client = SSHClient(host="example.com", user="testuser")

        # communicate() times out twice (still running), then finishes
        mock_process = MagicMock()
        mock_process.communicate.side_effect = [
            subprocess.TimeoutExpired("ssh", 1),
            subprocess.TimeoutExpired("ssh", 1),
            ("output", ""),
        ]
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        progress_calls = []
        result = client.run_with_progress(
            "echo test", timeout=60, on_progress=progress_calls.append
        )

        assert result.success is True
        assert result.stdout == "output"
        assert len(progress_calls) == 3

    @patch("subprocess.Popen")
    def test_timeout_kills_process(self, mock_popen):
        """Test that process is killed on timeout."""
        client = SSHClient(host="example.com", user="testuser")

        mock_process = MagicMock()
        mock_process.communicate.side_effect = [
            subprocess.TimeoutExpired("ssh", 1),
            ("partial output", ""),
        ]
        mock_popen.return_value = mock_process

        with patch("ssh.time.monotonic", side_effect=[0, 0, 100]):
            result = client.run_with_progress("sleep 1000", timeout=5)

        mock_process.kill.assert_called_once()
        assert result.success is False
        assert "timed out" in result.stderr
        assert result.stdout == "partial output"

    @patch("subprocess.Popen")
    def test_returns_stdout_stderr(self, mock_popen):
        """Test that stdout and stderr are captured."""
        client = SSHClient(host="example.com", user="testuser")

        mock_process = MagicMock()
        mock_process.communicate.return_value = ("stdout content", "stderr content")
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        result = client.run_with_progress("echo test", timeout=60)

        assert result.stdout == "stdout content"
        assert result.stderr == "stderr content"
        assert result.returncode == 0
