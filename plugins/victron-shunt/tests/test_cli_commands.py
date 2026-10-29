"""CLI command tests with BLE and MQTT faked out."""

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from victron_shunt import cli as cli_mod
from victron_shunt.reader import ShuntReading
from victron_shunt.scanner import BluetoothStatus, VictronDevice

KEY = "0123456789abcdef0123456789abcdef"
READING = ShuntReading(voltage=12.8, current=-2.5, soc=85.0, power=-32.0, consumed_ah=-1.0)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for var in ["VICTRON_ADDRESS", "VICTRON_KEY", "MQTT_HOST", "MQTT_USER", "MQTT_PASS"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("victron_shunt.config.CONFIG_PATHS", [tmp_path / "none.yaml"])
    monkeypatch.setattr(cli_mod, "CONFIG_PATHS", [tmp_path / "c.yaml"])
    monkeypatch.setattr("signal.signal", lambda *a: None)


@pytest.fixture
def fake_reader(monkeypatch):
    async def read_once(self, timeout=30.0):
        return READING

    async def read_continuous(self, callback, interval=1.0, stop_event=None):
        callback(READING)

    monkeypatch.setattr(cli_mod.VictronReader, "read_once", read_once)
    monkeypatch.setattr(cli_mod.VictronReader, "read_continuous", read_continuous)


@pytest.fixture
def fake_mqtt(monkeypatch):
    pub = MagicMock()
    import victron_shunt.mqtt as m

    monkeypatch.setattr(m, "MQTTPublisher", lambda cfg: pub)
    return pub


def run(*args, **env):
    return CliRunner().invoke(cli_mod.cli, list(args), env=env or None)


def test_read_once(fake_reader):
    result = run("read", "--address", "AA", "--key", KEY)
    assert result.exit_code == 0, result.output
    assert "12.8" in result.output


def test_read_bad_key_length():
    result = run("read", "--address", "AA", "--key", "abcd")
    assert result.exit_code == 1 and "Invalid key length" in result.output


def test_read_no_data(monkeypatch):
    async def none(self, timeout=30.0):
        return None

    monkeypatch.setattr(cli_mod.VictronReader, "read_once", none)
    result = run("read", "--address", "AA", "--key", KEY)
    assert result.exit_code == 1 and "No data received" in result.output


def test_read_continuous_with_mqtt(fake_reader, fake_mqtt):
    result = run("read", "-a", "AA", "-k", KEY, "--continuous", "--mqtt")
    assert result.exit_code == 0, result.output
    fake_mqtt.publish_reading.assert_called_once_with(READING)
    fake_mqtt.disconnect.assert_called_once()


def test_service_publishes(fake_reader, fake_mqtt):
    result = run("service", VICTRON_ADDRESS="AA", VICTRON_KEY=KEY)
    assert result.exit_code == 0, result.output
    fake_mqtt.publish_discovery.assert_called_once()
    fake_mqtt.publish_reading.assert_called_once_with(READING)


def test_service_mqtt_failure_continues(fake_reader, monkeypatch):
    import victron_shunt.mqtt as m

    broken = MagicMock()
    broken.connect.side_effect = RuntimeError("refused")
    monkeypatch.setattr(m, "MQTTPublisher", lambda cfg: broken)
    result = run("service", VICTRON_ADDRESS="AA", VICTRON_KEY=KEY)
    assert result.exit_code == 0, result.output
    broken.publish_reading.assert_not_called()


def test_service_invalid_config():
    result = run("service")
    assert result.exit_code == 1


def test_check_and_scan(monkeypatch):
    async def ok():
        return BluetoothStatus(available=True, adapter="hci0")

    async def devices(timeout):
        return [VictronDevice(name="SmartShunt", address="AA", rssi=-60, model="SmartShunt")]

    async def all_devices(timeout):
        return [("SmartShunt", "AA", -60)]

    monkeypatch.setattr(cli_mod, "check_bluetooth", ok)
    monkeypatch.setattr(cli_mod, "scan_victron_devices", devices)
    monkeypatch.setattr(cli_mod, "scan_all_devices", all_devices)
    assert run("check").exit_code == 0
    assert "AA" in run("scan", "-t", "0").output
    assert "SmartShunt" in run("scan", "--all", "-t", "0").output


def test_check_unavailable(monkeypatch):
    async def bad():
        return BluetoothStatus(available=False, error="no adapter")

    monkeypatch.setattr(cli_mod, "check_bluetooth", bad)
    result = run("check")
    assert result.exit_code == 1 and "no adapter" in result.output
