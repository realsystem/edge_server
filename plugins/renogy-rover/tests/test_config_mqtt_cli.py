"""Tests for renogy-rover config, MQTT publishing, scanner and CLI."""

import asyncio
import json
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from renogy_rover import scanner as scanner_mod
from renogy_rover.cli import cli
from renogy_rover.config import Config, MQTTConfig
from renogy_rover.reader import RoverReading


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in [
        "RENOGY_ADDRESS",
        "RENOGY_DEVICE_ID",
        "RENOGY_POLL_INTERVAL",
        "MQTT_HOST",
        "MQTT_PORT",
        "MQTT_USER",
        "MQTT_PASS",
    ]:
        monkeypatch.delenv(var, raising=False)


class TestConfig:
    def test_load_file_and_env_override(self, tmp_path, monkeypatch):
        f = tmp_path / "c.yaml"
        f.write_text(
            "address: AA:BB\ndevice_id: 1\npoll_interval: 10\n"
            "mqtt:\n  host: broker\n  port: 1884\n  user: u\n  password: p\n"
        )
        monkeypatch.setenv("RENOGY_POLL_INTERVAL", "60")
        monkeypatch.setenv("MQTT_PASS", "envpass")
        c = Config.load(f)
        assert (c.address, c.device_id, c.poll_interval) == ("AA:BB", 1, 60)
        assert (c.mqtt.host, c.mqtt.port, c.mqtt.user, c.mqtt.password) == (
            "broker",
            1884,
            "u",
            "envpass",
        )

    def test_save_is_private_and_omits_password(self, tmp_path):
        c = Config(address="AA", mqtt=MQTTConfig(user="u", password="secret"))
        path = tmp_path / "sub" / "c.yaml"
        c.save(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "secret" not in path.read_text()

    @pytest.mark.parametrize(
        "kwargs,error",
        [
            ({}, "address"),
            ({"address": "A", "device_id": 300}, "device_id"),
            ({"address": "A", "poll_interval": 0}, "poll_interval"),
        ],
    )
    def test_validate(self, kwargs, error):
        errors = Config(**kwargs).validate()
        assert any(e.startswith(error) for e in errors)
        assert Config(address="A").validate() == []


def reading():
    return RoverReading(
        battery_voltage=13.2,
        battery_current=4.5,
        battery_soc=87,
        pv_voltage=18.4,
        pv_current=3.3,
        pv_power=61,
        charge_state="mppt",
        controller_temp=25,
        daily_energy=158,
    )


class TestMQTT:
    @patch("renogy_rover.mqtt.mqtt")
    def test_connect_discovery_publish(self, mock_mqtt):
        from renogy_rover.mqtt import MQTTPublisher

        client = MagicMock()
        mock_mqtt.Client.return_value = client
        pub = MQTTPublisher(MQTTConfig(user="u", password="p", topic_prefix="t"))

        def fake_connect(*a, **k):
            pub._on_connect(client, None, None, 0)

        client.connect.side_effect = fake_connect
        pub.connect()
        client.username_pw_set.assert_called_once_with("u", "p")

        pub.publish_discovery()
        topics = [c.args[0] for c in client.publish.call_args_list]
        assert "homeassistant/sensor/renogy_rover/pv_power/config" in topics
        cfg = json.loads(client.publish.call_args_list[0].args[1])
        assert cfg["device"]["manufacturer"] == "Renogy"

        client.publish.reset_mock()
        pub.publish_reading(reading())
        published = {c.args[0]: c.args[1] for c in client.publish.call_args_list}
        assert published["t/pv_voltage"] == "18.40"
        assert published["t/charge_state"] == "mppt"
        assert json.loads(published["t/state"])["battery_soc"] == 87

        pub.disconnect()
        client.loop_stop.assert_called_once()

    @patch("renogy_rover.mqtt.mqtt")
    def test_connect_timeout(self, mock_mqtt):
        from renogy_rover.mqtt import MQTTPublisher

        with patch("renogy_rover.mqtt.time.sleep"):
            with pytest.raises(RuntimeError):
                MQTTPublisher(MQTTConfig()).connect()

    def test_publish_when_disconnected_is_noop(self):
        from renogy_rover import mqtt as mqtt_mod

        with patch.object(mqtt_mod, "mqtt", MagicMock()):
            pub = mqtt_mod.MQTTPublisher(MQTTConfig())
            pub.publish_reading(reading())  # no client yet: must not raise
            pub.publish_discovery()

    def test_missing_paho(self):
        from renogy_rover import mqtt as mqtt_mod

        with patch.object(mqtt_mod, "mqtt", None):
            with pytest.raises(RuntimeError):
                mqtt_mod.MQTTPublisher(MQTTConfig())


class TestScanner:
    def test_scan_all_devices_uses_advertisement_rssi(self, monkeypatch):
        dev_a = SimpleNamespace(name="BT-TH-1", address="A")
        dev_b = SimpleNamespace(name=None, address="B")
        adv_a = SimpleNamespace(rssi=-80, local_name=None)
        adv_b = SimpleNamespace(rssi=-40, local_name="Other")

        class FakeScanner:
            @staticmethod
            async def discover(timeout, return_adv):
                assert return_adv is True
                return {"A": (dev_a, adv_a), "B": (dev_b, adv_b)}

        monkeypatch.setattr(scanner_mod, "BleakScanner", FakeScanner)
        result = asyncio.run(scanner_mod.scan_all_devices(0.1))
        assert result == [("Other", "B", -40), ("BT-TH-1", "A", -80)]

    def test_scan_renogy_devices_filters(self, monkeypatch):
        class FakeScanner:
            def __init__(self, detection_callback):
                self.cb = detection_callback

            async def start(self):
                self.cb(SimpleNamespace(name="BT-TH-123", address="A"), SimpleNamespace(rssi=-50, local_name=None))
                self.cb(SimpleNamespace(name="BT-TH-123", address="A"), SimpleNamespace(rssi=-50, local_name=None))
                self.cb(SimpleNamespace(name="Phone", address="B"), SimpleNamespace(rssi=-30, local_name=None))

            async def stop(self):
                pass

        monkeypatch.setattr(scanner_mod, "BleakScanner", FakeScanner)
        devices = asyncio.run(scanner_mod.scan_renogy_devices(0))
        assert [(d.name, d.address) for d in devices] == [("BT-TH-123", "A")]

    def test_no_bleak(self, monkeypatch):
        monkeypatch.setattr(scanner_mod, "BleakScanner", None)
        assert asyncio.run(scanner_mod.scan_all_devices()) == []
        assert asyncio.run(scanner_mod.check_bluetooth()).available is False


class TestCLI:
    def test_version_works_without_install(self):
        result = CliRunner().invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_info(self):
        result = CliRunner().invoke(cli, ["info"])
        assert result.exit_code == 0 and "BT-2" in result.output

    def test_read_requires_address(self, tmp_path, monkeypatch):
        monkeypatch.setattr("renogy_rover.config.CONFIG_PATHS", [tmp_path / "none.yaml"])
        result = CliRunner().invoke(cli, ["read"])
        assert result.exit_code == 1
        assert "address required" in result.output

    def test_read_once_prints(self, monkeypatch):
        async def fake_read(self, timeout=30.0):
            return reading()

        monkeypatch.setattr("renogy_rover.cli.RenogyReader.__init__", lambda self, a, device_id=255: None)
        monkeypatch.setattr("renogy_rover.cli.RenogyReader.read_once", fake_read)
        result = CliRunner().invoke(cli, ["read", "--address", "AA"])
        assert result.exit_code == 0, result.output
        assert "18.40 V" in result.output and "mppt" in result.output

    def test_config_show(self, tmp_path, monkeypatch):
        monkeypatch.setattr("renogy_rover.cli.CONFIG_PATHS", [tmp_path / "c.yaml"])
        result = CliRunner().invoke(cli, ["config", "--show"])
        assert result.exit_code == 0 and "Configuration" in result.output

    def test_config_save(self, tmp_path, monkeypatch):
        target = tmp_path / "c.yaml"
        monkeypatch.setattr("renogy_rover.cli.CONFIG_PATHS", [target])
        result = CliRunner().invoke(cli, ["config", "--address", "AA:BB", "--device-id", "1"])
        assert result.exit_code == 0, result.output
        assert "AA:BB" in target.read_text()
