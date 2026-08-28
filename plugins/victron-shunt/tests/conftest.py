"""Pytest fixtures for victron-shunt tests."""

import pytest

from victron_shunt.config import Config, MQTTConfig
from victron_shunt.reader import ShuntReading


@pytest.fixture
def sample_config():
    """Create a sample config for testing."""
    return Config(
        address="AA:BB:CC:DD:EE:FF",
        key="0123456789abcdef0123456789abcdef",
        mqtt=MQTTConfig(
            host="localhost",
            port=1883,
            user="testuser",
            password="testpass",
        ),
    )


@pytest.fixture
def sample_reading():
    """Create a sample reading for testing."""
    return ShuntReading(
        voltage=12.85,
        current=-2.30,
        soc=87.5,
        power=-29.555,
        consumed_ah=15.2,
        time_remaining=390,
    )
