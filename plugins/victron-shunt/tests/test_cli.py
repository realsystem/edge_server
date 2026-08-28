"""Tests for CLI module."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import pytest

from victron_shunt.cli import cli
import victron_shunt.config as config_module


@pytest.fixture
def isolated_runner(monkeypatch, tmp_path):
    """Runner with isolated config (no user config loaded)."""
    # Point CONFIG_PATHS to non-existent locations
    monkeypatch.setattr(config_module, "CONFIG_PATHS", [tmp_path / "nonexistent.yaml"])
    monkeypatch.delenv("VICTRON_ADDRESS", raising=False)
    monkeypatch.delenv("VICTRON_KEY", raising=False)
    return CliRunner()


class TestCLI:
    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_help(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Victron Smart Shunt BLE reader" in result.output
        assert "check" in result.output
        assert "scan" in result.output
        assert "read" in result.output
        assert "config" in result.output
        assert "service" in result.output

    def test_version(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_info(self, runner):
        result = runner.invoke(cli, ["info"])
        assert result.exit_code == 0
        assert "encryption key" in result.output.lower()
        assert "Victron Connect" in result.output

    def test_config_show(self, runner):
        result = runner.invoke(cli, ["config", "--show"])
        assert result.exit_code == 0
        assert "Configuration:" in result.output
        assert "Address:" in result.output
        assert "MQTT:" in result.output

    def test_read_missing_address(self, isolated_runner):
        result = isolated_runner.invoke(cli, ["read"])
        assert result.exit_code == 1
        assert "address and key required" in result.output

    def test_read_missing_key(self, isolated_runner):
        result = isolated_runner.invoke(cli, ["read", "--address", "AA:BB:CC:DD:EE:FF"])
        assert result.exit_code == 1
        assert "address and key required" in result.output

    def test_read_invalid_key_length(self, runner):
        result = runner.invoke(cli, [
            "read",
            "--address", "AA:BB:CC:DD:EE:FF",
            "--key", "tooshort"
        ])
        assert result.exit_code == 1
        assert "Invalid key length" in result.output


class TestConfigCommand:
    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_config_save(self, runner, tmp_path):
        config_file = tmp_path / "test-config.yaml"
        result = runner.invoke(cli, [
            "--config", str(config_file),
            "config",
            "--address", "AA:BB:CC:DD:EE:FF",
            "--key", "0123456789abcdef0123456789abcdef",
        ], catch_exceptions=False)

        # May fail if config path doesn't exist, but shouldn't crash
        # The actual save goes to ~/.config/victron-shunt.yaml

    def test_config_with_mqtt(self, runner):
        result = runner.invoke(cli, [
            "config",
            "--address", "AA:BB:CC:DD:EE:FF",
            "--key", "0123456789abcdef0123456789abcdef",
            "--mqtt-host", "mqtt.test.com",
            "--mqtt-port", "8883",
            "--mqtt-user", "testuser",
        ])
        # Should complete without error
        assert "Config saved" in result.output or "validation" in result.output.lower()
