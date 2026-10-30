"""Tests for advertisement parsing, reader loops and scanner (BLE faked)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from victron_shunt import reader as reader_mod
from victron_shunt import scanner as scanner_mod
from victron_shunt.config import Config, MQTTConfig
from victron_shunt.reader import (
    VICTRON_MANUFACTURER_ID,
    ShuntReading,
    VictronReader,
    parse_advertisement,
)

KEY = "0123456789abcdef0123456789abcdef"


class FakeParsed:
    def __init__(self, voltage=12.8, current=-2.5, soc=85.0, consumed=-15.5, mins=390):
        self._v, self._c, self._s, self._ah, self._m = voltage, current, soc, consumed, mins

    def get_voltage(self):
        return self._v

    def get_current(self):
        return self._c

    def get_soc(self):
        return self._s

    def get_consumed_ah(self):
        return self._ah

    def get_remaining_mins(self):
        return self._m

    def get_model_name(self):
        return "SmartShunt 500A"


def fake_detect(parsed):
    class Device:
        def __init__(self, key):
            assert key == KEY

        def parse(self, raw):
            return parsed

    return lambda raw: Device if raw.startswith(b"\x10") else None


@pytest.fixture
def victron_ble(monkeypatch):
    def install(parsed=None):
        monkeypatch.setattr(reader_mod, "detect_device_type", fake_detect(parsed or FakeParsed()))

    return install


class TestParseAdvertisement:
    def test_parses_reading(self, victron_ble):
        victron_ble()
        r = parse_advertisement({VICTRON_MANUFACTURER_ID: b"\x10abc"}, KEY)
        assert r.voltage == 12.8 and r.current == -2.5 and r.soc == 85.0
        assert r.power == pytest.approx(-32.0)
        assert r.consumed_ah == -15.5 and r.time_remaining == 390
        assert r.raw_data == {"model": "SmartShunt 500A"}

    def test_other_manufacturer_ignored(self, victron_ble):
        victron_ble()
        assert parse_advertisement({0x004C: b"\x10abc"}, KEY) is None
        assert parse_advertisement({}, KEY) is None

    def test_unknown_device_type(self, victron_ble):
        victron_ble()
        assert parse_advertisement({VICTRON_MANUFACTURER_ID: b"\x99"}, KEY) is None

    def test_missing_methods_default(self, victron_ble):
        victron_ble(SimpleNamespace())
        r = parse_advertisement({VICTRON_MANUFACTURER_ID: b"\x10"}, KEY)
        assert (r.voltage, r.current, r.soc, r.consumed_ah) == (0.0, 0.0, 0.0, None)

    def test_requires_victron_ble(self, monkeypatch):
        monkeypatch.setattr(reader_mod, "detect_device_type", None)
        with pytest.raises(RuntimeError):
            parse_advertisement({VICTRON_MANUFACTURER_ID: b"\x10"}, KEY)


def make_scanner(events):
    """BleakScanner stand-in that replays (address, manufacturer_data) events."""

    class FakeScanner:
        def __init__(self, detection_callback):
            self.cb = detection_callback

        async def start(self):
            for address, mfr in events:
                self.cb(SimpleNamespace(address=address), SimpleNamespace(manufacturer_data=mfr))

        async def stop(self):
            pass

    return FakeScanner


class TestVictronReader:
    def test_read_once_filters_address(self, monkeypatch, victron_ble):
        victron_ble()
        events = [
            ("11:22:33:44:55:66", {VICTRON_MANUFACTURER_ID: b"\x10"}),
            ("aa:bb:cc:dd:ee:ff", {VICTRON_MANUFACTURER_ID: b"\x10"}),
        ]
        monkeypatch.setattr(reader_mod, "BleakScanner", make_scanner(events))
        r = asyncio.run(VictronReader("AA:BB:CC:DD:EE:FF", KEY).read_once(timeout=1))
        assert isinstance(r, ShuntReading) and r.voltage == 12.8

    def test_read_once_timeout(self, monkeypatch, victron_ble):
        victron_ble()
        monkeypatch.setattr(reader_mod, "BleakScanner", make_scanner([]))
        assert asyncio.run(VictronReader("AA", KEY).read_once(timeout=0.05)) is None

    def test_parse_errors_are_swallowed(self, monkeypatch):
        def boom(raw):
            raise ValueError("bad key")

        monkeypatch.setattr(reader_mod, "detect_device_type", boom)
        monkeypatch.setattr(
            reader_mod, "BleakScanner", make_scanner([("AA", {VICTRON_MANUFACTURER_ID: b"\x10"})])
        )
        assert asyncio.run(VictronReader("AA", KEY).read_once(timeout=0.05)) is None

    def test_read_continuous_deduplicates(self, monkeypatch, victron_ble):
        victron_ble()
        events = [("AA", {VICTRON_MANUFACTURER_ID: b"\x10"})] * 3
        monkeypatch.setattr(reader_mod, "BleakScanner", make_scanner(events))
        seen = []

        async def go():
            stop = asyncio.Event()
            stop.set()
            await VictronReader("AA", KEY).read_continuous(seen.append, 0.01, stop)

        asyncio.run(go())
        assert len(seen) == 1

    def test_missing_deps(self, monkeypatch):
        monkeypatch.setattr(reader_mod, "BleakScanner", None)
        with pytest.raises(RuntimeError):
            asyncio.run(VictronReader("AA", KEY).read_once())


class TestScanner:
    def test_scan_all_devices_uses_advertisement_rssi(self, monkeypatch):
        class FakeScanner:
            @staticmethod
            async def discover(timeout, return_adv):
                return {
                    "A": (SimpleNamespace(name="SmartShunt", address="A"), SimpleNamespace(rssi=-70, local_name=None)),
                    "B": (SimpleNamespace(name=None, address="B"), SimpleNamespace(rssi=None, local_name=None)),
                }

        monkeypatch.setattr(scanner_mod, "BleakScanner", FakeScanner)
        assert asyncio.run(scanner_mod.scan_all_devices(0)) == [
            ("SmartShunt", "A", -70),
            ("(unknown)", "B", -100),
        ]

    def test_scan_victron_devices(self, monkeypatch):
        class FakeScanner:
            def __init__(self, detection_callback):
                self.cb = detection_callback

            async def start(self):
                self.cb(SimpleNamespace(name="SmartShunt HQ1", address="A"), SimpleNamespace(rssi=-60, local_name=None))
                self.cb(SimpleNamespace(name=None, address="B"), SimpleNamespace(rssi=-50, local_name="BMV-712"))
                self.cb(SimpleNamespace(name="TV", address="C"), SimpleNamespace(rssi=-10, local_name=None))

            async def stop(self):
                pass

        monkeypatch.setattr(scanner_mod, "BleakScanner", FakeScanner)
        devices = asyncio.run(scanner_mod.scan_victron_devices(0))
        assert [(d.address, d.model) for d in devices] == [("B", "BMV"), ("A", "SmartShunt")]

    def test_check_bluetooth_errors(self, monkeypatch):
        class Broken:
            async def start(self):
                raise OSError("org.bluez.Error.NotReady")

        monkeypatch.setattr(scanner_mod, "BleakScanner", Broken)
        status = asyncio.run(scanner_mod.check_bluetooth())
        assert status.available is False and status.error


class TestConfigKeyValidation:
    @pytest.mark.parametrize(
        "key,ok",
        [
            (KEY, True),
            ("01 23 45 67 89 ab cd ef 01 23 45 67 89 ab cd ef", True),
            ("zz23456789abcdef0123456789abcdef", False),  # not hex
            ("0123", False),
        ],
    )
    def test_key(self, key, ok):
        errors = Config(address="AA", key=key).validate()
        assert (errors == []) is ok


@patch("victron_shunt.mqtt.mqtt")
def test_consumed_ah_is_measurement(mock_mqtt):
    from victron_shunt.mqtt import MQTTPublisher

    pub = MQTTPublisher(MQTTConfig())
    pub.client = MagicMock()
    pub.publish_discovery()
    payloads = {
        c.args[0]: json.loads(c.args[1]) for c in pub.client.publish.call_args_list
    }
    consumed = payloads["homeassistant/sensor/victron_smartshunt/consumed_ah/config"]
    assert consumed["state_class"] == "measurement"
