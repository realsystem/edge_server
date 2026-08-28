"""Tests for MQTT module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from victron_shunt.config import MQTTConfig
from victron_shunt.reader import ShuntReading


class TestMQTTPublisher:
    @pytest.fixture
    def mqtt_config(self):
        return MQTTConfig(
            host="localhost",
            port=1883,
            user="testuser",
            password="testpass",
            topic_prefix="victron/test",
        )

    @pytest.fixture
    def sample_reading(self):
        return ShuntReading(
            voltage=12.85,
            current=-2.30,
            soc=87.5,
            power=-29.555,
            consumed_ah=15.2,
            time_remaining=390,
        )

    def test_import(self):
        from victron_shunt.mqtt import MQTTPublisher
        assert MQTTPublisher is not None

    @patch("victron_shunt.mqtt.mqtt")
    def test_publish_reading(self, mock_mqtt_module, mqtt_config, sample_reading):
        from victron_shunt.mqtt import MQTTPublisher

        mock_client = MagicMock()
        mock_mqtt_module.Client.return_value = mock_client

        publisher = MQTTPublisher(mqtt_config)
        publisher.client = mock_client
        publisher._connected = True

        publisher.publish_reading(sample_reading)

        # Check individual topic publishes
        calls = mock_client.publish.call_args_list
        topics_published = {call[0][0]: call[0][1] for call in calls}

        assert "victron/test/voltage" in topics_published
        assert topics_published["victron/test/voltage"] == "12.85"

        assert "victron/test/current" in topics_published
        assert topics_published["victron/test/current"] == "-2.30"

        assert "victron/test/soc" in topics_published
        assert topics_published["victron/test/soc"] == "87.5"

        assert "victron/test/power" in topics_published
        assert topics_published["victron/test/power"] == "-29.6"

        # Check JSON state topic
        assert "victron/test/state" in topics_published
        state_json = json.loads(topics_published["victron/test/state"])
        assert state_json["voltage"] == 12.85
        assert state_json["current"] == -2.3
        assert state_json["soc"] == 87.5

    @patch("victron_shunt.mqtt.mqtt")
    def test_publish_discovery(self, mock_mqtt_module, mqtt_config):
        from victron_shunt.mqtt import MQTTPublisher

        mock_client = MagicMock()
        mock_mqtt_module.Client.return_value = mock_client

        publisher = MQTTPublisher(mqtt_config, device_name="testshunt")
        publisher.client = mock_client
        publisher._connected = True

        publisher.publish_discovery()

        # Should publish discovery config for each sensor
        calls = mock_client.publish.call_args_list
        discovery_topics = [call[0][0] for call in calls if "homeassistant/sensor" in call[0][0]]

        assert len(discovery_topics) >= 4  # voltage, current, power, soc at minimum

        # Verify discovery payload structure
        for call in calls:
            topic, payload = call[0][0], call[0][1]
            if "homeassistant/sensor" in topic:
                config = json.loads(payload)
                assert "name" in config
                assert "unique_id" in config
                assert "state_topic" in config
                assert "device" in config

    @patch("victron_shunt.mqtt.mqtt")
    def test_not_connected_no_publish(self, mock_mqtt_module, mqtt_config, sample_reading):
        from victron_shunt.mqtt import MQTTPublisher

        mock_client = MagicMock()
        mock_mqtt_module.Client.return_value = mock_client

        publisher = MQTTPublisher(mqtt_config)
        publisher.client = mock_client
        publisher._connected = False

        publisher.publish_reading(sample_reading)

        mock_client.publish.assert_not_called()
