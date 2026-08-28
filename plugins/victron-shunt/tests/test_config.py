"""Tests for config module."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from victron_shunt.config import Config, MQTTConfig


class TestMQTTConfig:
    def test_defaults(self):
        cfg = MQTTConfig()
        assert cfg.host == "localhost"
        assert cfg.port == 1883
        assert cfg.user is None
        assert cfg.password is None
        assert cfg.topic_prefix == "victron/smartshunt"


class TestConfig:
    def test_defaults(self):
        cfg = Config()
        assert cfg.address is None
        assert cfg.key is None
        assert isinstance(cfg.mqtt, MQTTConfig)

    def test_load_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({
                "address": "AA:BB:CC:DD:EE:FF",
                "key": "0123456789abcdef0123456789abcdef",
                "mqtt": {
                    "host": "mqtt.example.com",
                    "port": 8883,
                    "user": "testuser",
                }
            }, f)
            f.flush()

            try:
                cfg = Config.load(Path(f.name))
                assert cfg.address == "AA:BB:CC:DD:EE:FF"
                assert cfg.key == "0123456789abcdef0123456789abcdef"
                assert cfg.mqtt.host == "mqtt.example.com"
                assert cfg.mqtt.port == 8883
                assert cfg.mqtt.user == "testuser"
            finally:
                os.unlink(f.name)

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("VICTRON_ADDRESS", "11:22:33:44:55:66")
        monkeypatch.setenv("VICTRON_KEY", "abcdef0123456789abcdef0123456789")
        monkeypatch.setenv("MQTT_HOST", "env.mqtt.com")
        monkeypatch.setenv("MQTT_PORT", "1884")
        monkeypatch.setenv("MQTT_USER", "envuser")
        monkeypatch.setenv("MQTT_PASS", "envpass")

        cfg = Config.load()
        assert cfg.address == "11:22:33:44:55:66"
        assert cfg.key == "abcdef0123456789abcdef0123456789"
        assert cfg.mqtt.host == "env.mqtt.com"
        assert cfg.mqtt.port == 1884
        assert cfg.mqtt.user == "envuser"
        assert cfg.mqtt.password == "envpass"

    def test_env_overrides_file(self, monkeypatch):
        """Env vars should override file values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({
                "address": "AA:BB:CC:DD:EE:FF",
                "key": "0123456789abcdef0123456789abcdef",
            }, f)
            f.flush()

            try:
                monkeypatch.setenv("VICTRON_ADDRESS", "OVERRIDE:AD:DR:ES:S1")
                cfg = Config.load(Path(f.name))
                assert cfg.address == "OVERRIDE:AD:DR:ES:S1"
                assert cfg.key == "0123456789abcdef0123456789abcdef"
            finally:
                os.unlink(f.name)

    def test_validate_missing_address(self):
        cfg = Config(key="0123456789abcdef0123456789abcdef")
        errors = cfg.validate()
        assert len(errors) == 1
        assert "address" in errors[0]

    def test_validate_missing_key(self):
        cfg = Config(address="AA:BB:CC:DD:EE:FF")
        errors = cfg.validate()
        assert len(errors) == 1
        assert "key" in errors[0]

    def test_validate_invalid_key_length(self):
        cfg = Config(address="AA:BB:CC:DD:EE:FF", key="tooshort")
        errors = cfg.validate()
        assert len(errors) == 1
        assert "32 hex" in errors[0]

    def test_validate_valid(self):
        cfg = Config(
            address="AA:BB:CC:DD:EE:FF",
            key="0123456789abcdef0123456789abcdef"
        )
        errors = cfg.validate()
        assert len(errors) == 0

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"

            cfg = Config(
                address="AA:BB:CC:DD:EE:FF",
                key="0123456789abcdef0123456789abcdef",
            )
            cfg.mqtt.host = "saved.mqtt.com"
            cfg.mqtt.user = "saveduser"
            cfg.save(path)

            # Check file permissions (should be 600)
            assert (path.stat().st_mode & 0o777) == 0o600

            # Reload and verify
            loaded = Config.load(path)
            assert loaded.address == cfg.address
            assert loaded.key == cfg.key
            assert loaded.mqtt.host == "saved.mqtt.com"
            assert loaded.mqtt.user == "saveduser"
