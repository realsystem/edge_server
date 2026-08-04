"""Tests for SSH module."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

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
        client = SSHClient(
            host="example.com", user="testuser", key_file="/path/to/key"
        )
        args = client._ssh_args()

        assert "-i" in args
        assert "/path/to/key" in args


class TestSSHRunWithProgress:
    """Tests for run_with_progress method."""

    @patch("subprocess.Popen")
    def test_calls_progress_callback(self, mock_popen):
        """Test that progress callback is called during execution."""
        client = SSHClient(host="example.com", user="testuser")

        # Mock process that finishes after 2 polls
        mock_process = MagicMock()
        poll_count = [0]

        def mock_poll():
            poll_count[0] += 1
            return None if poll_count[0] < 3 else 0

        mock_process.poll = mock_poll
        mock_process.communicate.return_value = ("output", "")
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        progress_calls = []

        def on_progress(elapsed):
            progress_calls.append(elapsed)

        with patch("time.sleep"):  # Skip actual sleeping
            result = client.run_with_progress(
                "echo test", timeout=60, on_progress=on_progress
            )

        assert result.success is True
        assert len(progress_calls) >= 2  # Progress was called multiple times

    @patch("subprocess.Popen")
    def test_timeout_kills_process(self, mock_popen):
        """Test that process is killed on timeout."""
        client = SSHClient(host="example.com", user="testuser")

        # Mock process that never finishes
        mock_process = MagicMock()
        mock_process.poll.return_value = None  # Never done
        mock_process.communicate.return_value = ("partial output", "")
        mock_popen.return_value = mock_process

        with patch("time.sleep"):
            with patch("time.monotonic") as mock_time:
                # Simulate time passing beyond timeout
                mock_time.side_effect = [0, 0, 100, 100]  # Start, check, elapsed > timeout
                result = client.run_with_progress("sleep 1000", timeout=5)

        mock_process.kill.assert_called_once()
        assert result.success is False
        assert "timed out" in result.stderr

    @patch("subprocess.Popen")
    def test_returns_stdout_stderr(self, mock_popen):
        """Test that stdout and stderr are captured."""
        client = SSHClient(host="example.com", user="testuser")

        mock_process = MagicMock()
        mock_process.poll.return_value = 0  # Done immediately
        mock_process.communicate.return_value = ("stdout content", "stderr content")
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        result = client.run_with_progress("echo test", timeout=60)

        assert result.stdout == "stdout content"
        assert result.stderr == "stderr content"
        assert result.returncode == 0
