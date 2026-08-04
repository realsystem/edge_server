"""Tests for config module."""

import tempfile
from pathlib import Path

import pytest

from config import Config, TimeoutConfig, NetworkConfig, ServiceConfig


class TestTimeoutConfig:
    """Tests for TimeoutConfig."""

    def test_defaults(self):
        """Test default timeout values."""
        config = TimeoutConfig()
        assert config.discovery == 30
        assert config.prerequisites == 60
        assert config.deployment_base == 600
        assert config.probe_interval == 5

    def test_custom_values(self):
        """Test custom timeout values."""
        config = TimeoutConfig(discovery=60, deployment_base=1200)
        assert config.discovery == 60
        assert config.deployment_base == 1200


class TestConfig:
    """Tests for Config."""

    def test_default_config(self):
        """Test default configuration."""
        config = Config()
        assert config.timeouts.discovery == 30
        assert config.network.default_dns == "8.8.8.8"
        assert config.services.ha_port == 8123
        assert config.rollback.enabled is True

    def test_load_nonexistent_file(self):
        """Test loading non-existent config file returns defaults."""
        config = Config.load(Path("/nonexistent/config.cfg"))
        assert config.timeouts.discovery == 30

    def test_load_config_file(self):
        """Test loading config from file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False) as f:
            f.write("""
[timeouts]
DISCOVERY = 60
DEPLOYMENT_BASE = 1200

[network]
DEFAULT_DNS = 1.1.1.1

[services]
HA_PORT = 9123
VERIFY_FRIGATE = false
""")
            f.flush()
            config = Config.load(Path(f.name))

        assert config.timeouts.discovery == 60
        assert config.timeouts.deployment_base == 1200
        assert config.network.default_dns == "1.1.1.1"
        assert config.services.ha_port == 9123
        assert config.services.verify_frigate is False

    def test_save_config(self):
        """Test saving config to file."""
        config = Config()
        config.timeouts.discovery = 120
        config.network.default_dns = "9.9.9.9"

        with tempfile.NamedTemporaryFile(suffix=".cfg", delete=False) as f:
            config.save(Path(f.name))
            loaded = Config.load(Path(f.name))

        assert loaded.timeouts.discovery == 120
        assert loaded.network.default_dns == "9.9.9.9"

    def test_partial_config_file(self):
        """Test loading partial config file with defaults for missing values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False) as f:
            f.write("""
[timeouts]
DISCOVERY = 45
""")
            f.flush()
            config = Config.load(Path(f.name))

        assert config.timeouts.discovery == 45
        assert config.timeouts.deployment_base == 600  # default
        assert config.services.ha_port == 8123  # default
