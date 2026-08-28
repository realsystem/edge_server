"""Tests for scanner module."""

import pytest

from victron_shunt.scanner import BluetoothStatus, VictronDevice, VICTRON_PREFIXES


class TestBluetoothStatus:
    def test_available(self):
        status = BluetoothStatus(available=True, adapter="hci0")
        assert status.available is True
        assert status.adapter == "hci0"
        assert status.error is None

    def test_unavailable(self):
        status = BluetoothStatus(available=False, error="No adapter found")
        assert status.available is False
        assert status.error == "No adapter found"


class TestVictronDevice:
    def test_device_fields(self):
        device = VictronDevice(
            name="SmartShunt HQ123",
            address="AA:BB:CC:DD:EE:FF",
            rssi=-65,
            model="SmartShunt",
        )
        assert device.name == "SmartShunt HQ123"
        assert device.address == "AA:BB:CC:DD:EE:FF"
        assert device.rssi == -65
        assert device.model == "SmartShunt"


class TestVictronPrefixes:
    def test_known_prefixes(self):
        assert "Smart Shunt" in VICTRON_PREFIXES
        assert "SmartShunt" in VICTRON_PREFIXES
        assert "BMV" in VICTRON_PREFIXES

    def test_prefix_matching(self):
        test_names = [
            ("SmartShunt HQ2050ABC", True),
            ("Smart Shunt 500A", True),
            ("BMV-712", True),
            ("Random Device", False),
            ("iPhone", False),
        ]
        for name, should_match in test_names:
            matches = any(name.startswith(prefix) for prefix in VICTRON_PREFIXES)
            assert matches == should_match, f"Name '{name}' match={matches}, expected={should_match}"
