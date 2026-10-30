"""CLI command tests with BLE and MQTT faked out."""

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from renogy_rover import cli as cli_mod
from renogy_rover.reader import RoverReading
from renogy_rover.scanner import BluetoothStatus, RenogyDevice

READING = RoverReading(battery_voltage=13.2, battery_current=4.5, battery_soc=87, load_power=5)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for var in ["RENOGY_ADDRESS", "MQTT_HOST", "MQTT_USER", "MQTT_PASS"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("renogy_rover.config.CONFIG_PATHS", [tmp_path / "none.yaml"])
    monkeypatch.setattr("signal.signal", lambda *a: None)
    monkeypatch.setattr(cli_mod.RenogyReader, "__init__", lambda self, a, device_id=255: None)


@pytest.fixture
def fake_reader(monkeypatch):
    async def read_once(self, timeout=30.0):
        return READING

    async def poll(self, callback, interval=30.0, stop_event=None):
        callback(READING)

    monkeypatch.setattr(cli_mod.RenogyReader, "read_once", read_once)
    monkeypatch.setattr(cli_mod.RenogyReader, "poll_continuous", poll)


@pytest.fixture
def fake_mqtt(monkeypatch):
    import renogy_rover.mqtt as m

    pub = MagicMock()
    monkeypatch.setattr(m, "MQTTPublisher", lambda cfg: pub)
    return pub


def run(*args, **env):
    return CliRunner().invoke(cli_mod.cli, list(args), env=env or None)


def test_read_with_mqtt(fake_reader, fake_mqtt):
    result = run("read", "--address", "AA", "--mqtt")
    assert result.exit_code == 0, result.output
    assert "Load:" in result.output
    fake_mqtt.publish_reading.assert_called_once_with(READING)


def test_read_no_data(monkeypatch):
    async def none(self, timeout=30.0):
        return None

    monkeypatch.setattr(cli_mod.RenogyReader, "read_once", none)
    result = run("read", "--address", "AA")
    assert result.exit_code == 1 and "No data received" in result.output


def test_read_continuous(fake_reader):
    result = run("read", "--address", "AA", "--continuous")
    assert result.exit_code == 0, result.output
    assert "Bat:" in result.output


def test_service(fake_reader, fake_mqtt):
    result = run("service", RENOGY_ADDRESS="AA")
    assert result.exit_code == 0, result.output
    fake_mqtt.publish_discovery.assert_called_once()
    fake_mqtt.publish_reading.assert_called_once_with(READING)


def test_service_no_mqtt(fake_reader, fake_mqtt):
    result = run("service", "--no-mqtt", RENOGY_ADDRESS="AA")
    assert result.exit_code == 0
    fake_mqtt.connect.assert_not_called()


def test_service_requires_address():
    assert run("service").exit_code == 1


def test_check_and_scan(monkeypatch):
    async def ok():
        return BluetoothStatus(available=True, adapter="hci0")

    async def devices(timeout):
        return [RenogyDevice(name="BT-TH-1", address="AA", rssi=-60)]

    async def all_devices(timeout):
        return [("BT-TH-1", "AA", -60)]

    monkeypatch.setattr(cli_mod, "check_bluetooth", ok)
    monkeypatch.setattr(cli_mod, "scan_renogy_devices", devices)
    monkeypatch.setattr(cli_mod, "scan_all_devices", all_devices)
    assert run("check").exit_code == 0
    assert "AA" in run("scan", "-t", "0").output
    assert "BT-TH-1" in run("scan", "--all", "-t", "0").output


def test_scan_nothing(monkeypatch):
    async def none(timeout):
        return []

    monkeypatch.setattr(cli_mod, "scan_renogy_devices", none)
    assert run("scan", "-t", "0").exit_code == 1
