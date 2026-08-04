"""Tests for bootstrap module."""

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from bootstrap import Bootstrap, main
from config import Config
from ssh import SSHResult


class TestBootstrap:
    """Tests for Bootstrap class."""

    def test_init(self):
        """Test Bootstrap initialization."""
        config = Config()
        config.ssh.user = "testuser"

        bootstrap = Bootstrap(
            target="192.168.1.100",
            config=config,
            mode="auto",
            deploy_type="full",
            dry_run=True,
        )

        assert bootstrap.target == "192.168.1.100"
        assert bootstrap.mode == "auto"
        assert bootstrap.deploy_type == "full"
        assert bootstrap.dry_run is True

    def test_dry_run_discovery(self):
        """Test dry run discovery phase."""
        config = Config()
        config.ssh.user = "testuser"

        bootstrap = Bootstrap(
            target="192.168.1.100",
            config=config,
            mode="auto",
            dry_run=True,
        )

        # Dry run should succeed without actual SSH
        result = bootstrap._phase_discovery()
        assert result is True

    def test_dry_run_prerequisites(self):
        """Test dry run prerequisites phase."""
        config = Config()
        config.ssh.user = "testuser"

        bootstrap = Bootstrap(
            target="192.168.1.100",
            config=config,
            mode="auto",
            dry_run=True,
        )

        # Run discovery first
        bootstrap._phase_discovery()

        result = bootstrap._phase_prerequisites()
        assert result is True

    def test_skip_init(self):
        """Test skipping initial setup."""
        config = Config()
        config.ssh.user = "testuser"

        bootstrap = Bootstrap(
            target="192.168.1.100",
            config=config,
            mode="auto",
            dry_run=True,
            skip_init=True,
        )

        result = bootstrap._phase_initial_setup()
        assert result is True


class TestMain:
    """Tests for main() CLI entry point."""

    def test_help(self):
        """Test --help flag."""
        with patch.object(sys, "argv", ["bootstrap", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_missing_target(self):
        """Test error when target is missing."""
        with patch.object(sys, "argv", ["bootstrap"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code != 0

    def test_dry_run_flag(self):
        """Test --dry-run flag is recognized."""
        with patch.object(
            sys, "argv", ["bootstrap", "--dry-run", "--auto", "--skip-init", "192.0.2.1"]
        ):
            # This will try to run but fail on SSH - that's OK for this test
            # We just want to verify the flag is parsed
            result = main()
            # Dry run should succeed
            assert result == 0

    def test_invalid_deploy_type(self):
        """Test invalid --deploy type."""
        with patch.object(sys, "argv", ["bootstrap", "--deploy", "invalid", "192.168.1.100"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code != 0

    def test_timeout_override(self):
        """Test --timeout override parsing."""
        with patch.object(
            sys,
            "argv",
            [
                "bootstrap",
                "--dry-run",
                "--auto",
                "--skip-init",
                "--timeout",
                "discovery=120",
                "--timeout",
                "deployment_base=1800",
                "192.0.2.1",
            ],
        ):
            # Capture the config that gets created
            original_bootstrap_init = Bootstrap.__init__

            captured_config = None

            def capture_init(self, *args, **kwargs):
                nonlocal captured_config
                captured_config = kwargs.get("config")
                original_bootstrap_init(self, *args, **kwargs)

            with patch.object(Bootstrap, "__init__", capture_init):
                with patch.object(Bootstrap, "run", return_value=True):
                    main()

            assert captured_config is not None
            assert captured_config.timeouts.discovery == 120
            assert captured_config.timeouts.deployment_base == 1800


class TestDeploymentPhase:
    """Tests for deployment phase with progress output."""

    def test_deployment_shows_progress(self, capsys):
        """Test that deployment phase shows progress spinner."""
        config = Config()
        config.ssh.user = "testuser"
        config.timeouts.deployment_base = 10

        bootstrap = Bootstrap(
            target="192.168.1.100",
            config=config,
            mode="auto",
            deploy_type="base",
            dry_run=False,
        )

        # Mock SSH client
        mock_ssh = MagicMock()
        bootstrap.ssh = mock_ssh

        # Mock snapshots
        mock_snapshots = MagicMock()
        bootstrap.snapshots = mock_snapshots
        bootstrap.config.rollback.snapshot_before_deploy = False

        # Track progress calls
        progress_calls = []

        def mock_run_with_progress(command, timeout=None, on_progress=None, status_file=None):
            # Simulate calling progress a few times
            if on_progress:
                for i in range(3):
                    on_progress(float(i), f"Step {i}")
                    progress_calls.append(i)
            # Return successful result with some output
            return SSHResult(
                0,
                "2024-08-04 [INFO] Starting deployment\n"
                "2024-08-04 [OK] Docker installed\n"
                "2024-08-04 [OK] Services started\n",
                "",
            )

        mock_ssh.run_with_progress = mock_run_with_progress

        # Run deployment phase
        result = bootstrap._phase_deployment()

        assert result is True
        assert len(progress_calls) == 3  # Progress was called

        # Check output contains summary lines
        captured = capsys.readouterr()
        assert "[OK]" in captured.out or "[INFO]" in captured.out

    def test_deployment_shows_output_on_success(self, capsys):
        """Test that deployment shows [OK]/[INFO] lines after completion."""
        config = Config()
        config.ssh.user = "testuser"
        config.timeouts.deployment_base = 10

        bootstrap = Bootstrap(
            target="192.168.1.100",
            config=config,
            mode="auto",
            deploy_type="base",
            dry_run=False,
        )

        mock_ssh = MagicMock()
        bootstrap.ssh = mock_ssh
        mock_snapshots = MagicMock()
        bootstrap.snapshots = mock_snapshots
        bootstrap.config.rollback.snapshot_before_deploy = False

        mock_ssh.run_with_progress.return_value = SSHResult(
            0,
            "2024-08-04 [INFO] Installing Docker\n"
            "2024-08-04 [OK] Docker ready\n"
            "2024-08-04 [INFO] Starting containers\n"
            "2024-08-04 [OK] All services running\n",
            "",
        )

        result = bootstrap._phase_deployment()

        assert result is True
        captured = capsys.readouterr()
        # Should show the [OK] and [INFO] lines
        assert "Docker" in captured.out or "services" in captured.out

    def test_deployment_shows_error_on_failure(self, capsys):
        """Test that deployment shows error output on failure."""
        config = Config()
        config.ssh.user = "testuser"
        config.timeouts.deployment_base = 10

        bootstrap = Bootstrap(
            target="192.168.1.100",
            config=config,
            mode="auto",
            deploy_type="base",
            dry_run=False,
        )

        mock_ssh = MagicMock()
        bootstrap.ssh = mock_ssh
        mock_snapshots = MagicMock()
        bootstrap.snapshots = mock_snapshots
        bootstrap.config.rollback.snapshot_before_deploy = False

        mock_ssh.run_with_progress.return_value = SSHResult(
            1,
            "2024-08-04 [INFO] Installing Docker\n"
            "2024-08-04 [ERROR] Failed to pull image\n",
            "docker: Error response from daemon",
        )

        result = bootstrap._phase_deployment()

        assert result is False
        captured = capsys.readouterr()
        # Should show error details
        assert "failed" in captured.out.lower() or "error" in captured.out.lower()
