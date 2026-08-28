"""Tests for reader module."""

import pytest

from victron_shunt.reader import ShuntReading, VictronReader


class TestShuntReading:
    def test_dataclass_fields(self):
        reading = ShuntReading(
            voltage=12.8,
            current=-2.5,
            soc=85.0,
            power=-32.0,
            consumed_ah=15.5,
            time_remaining=390,
        )
        assert reading.voltage == 12.8
        assert reading.current == -2.5
        assert reading.soc == 85.0
        assert reading.power == -32.0
        assert reading.consumed_ah == 15.5
        assert reading.time_remaining == 390

    def test_optional_fields(self):
        reading = ShuntReading(
            voltage=12.8,
            current=-2.5,
            soc=85.0,
            power=-32.0,
        )
        assert reading.consumed_ah is None
        assert reading.time_remaining is None
        assert reading.raw_data is None


class TestVictronReader:
    def test_init(self):
        reader = VictronReader("AA:BB:CC:DD:EE:FF", "0123456789abcdef0123456789abcdef")
        assert reader.address == "AA:BB:CC:DD:EE:FF"
        assert reader.encryption_key == "0123456789abcdef0123456789abcdef"

    def test_address_uppercase(self):
        reader = VictronReader("aa:bb:cc:dd:ee:ff", "0123456789abcdef0123456789abcdef")
        assert reader.address == "AA:BB:CC:DD:EE:FF"
