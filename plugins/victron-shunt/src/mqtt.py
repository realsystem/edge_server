"""MQTT publishing for Victron Smart Shunt readings."""

import json
import time
from typing import Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from .config import MQTTConfig
from .reader import ShuntReading


class MQTTPublisher:
    """Publish battery readings to MQTT broker."""

    def __init__(self, config: MQTTConfig, device_name: str = "smartshunt"):
        if mqtt is None:
            raise RuntimeError("paho-mqtt not installed (pip install paho-mqtt)")

        self.config = config
        self.device_name = device_name
        self.client: Optional[mqtt.Client] = None
        self._connected = False

    def connect(self) -> None:
        """Connect to MQTT broker."""
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        if self.config.user and self.config.password:
            self.client.username_pw_set(self.config.user, self.config.password)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

        self.client.connect(self.config.host, self.config.port, keepalive=60)
        self.client.loop_start()

        # Wait for connection
        for _ in range(50):
            if self._connected:
                break
            time.sleep(0.1)

        if not self._connected:
            raise RuntimeError(f"Failed to connect to MQTT broker {self.config.host}:{self.config.port}")

    def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self._connected = False

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = reason_code == 0

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = False

    def publish_discovery(self) -> None:
        """Publish Home Assistant MQTT discovery config."""
        if not self.client:
            return

        device_id = f"victron_{self.device_name}"
        device_info = {
            "identifiers": [device_id],
            "name": f"Victron {self.device_name.replace('_', ' ').title()}",
            "manufacturer": "Victron Energy",
            "model": "Smart Shunt",
        }

        sensors = [
            ("voltage", "V", "voltage", "measurement", "mdi:flash"),
            ("current", "A", "current", "measurement", "mdi:current-dc"),
            ("power", "W", "power", "measurement", "mdi:lightning-bolt"),
            ("soc", "%", "battery", "measurement", "mdi:battery"),
            ("consumed_ah", "Ah", None, "total_increasing", "mdi:battery-minus"),
        ]

        for name, unit, device_class, state_class, icon in sensors:
            config_topic = f"homeassistant/sensor/{device_id}/{name}/config"
            state_topic = f"{self.config.topic_prefix}/{name}"

            payload = {
                "name": name.replace("_", " ").title(),
                "unique_id": f"{device_id}_{name}",
                "state_topic": state_topic,
                "unit_of_measurement": unit,
                "state_class": state_class,
                "icon": icon,
                "device": device_info,
            }

            if device_class:
                payload["device_class"] = device_class

            self.client.publish(config_topic, json.dumps(payload), retain=True)

    def publish_reading(self, reading: ShuntReading) -> None:
        """Publish a battery reading to MQTT."""
        if not self.client or not self._connected:
            return

        prefix = self.config.topic_prefix

        self.client.publish(f"{prefix}/voltage", f"{reading.voltage:.2f}")
        self.client.publish(f"{prefix}/current", f"{reading.current:.2f}")
        self.client.publish(f"{prefix}/power", f"{reading.power:.1f}")
        self.client.publish(f"{prefix}/soc", f"{reading.soc:.1f}")

        if reading.consumed_ah is not None:
            self.client.publish(f"{prefix}/consumed_ah", f"{reading.consumed_ah:.1f}")

        # Publish combined JSON for debugging
        data = {
            "voltage": round(reading.voltage, 2),
            "current": round(reading.current, 2),
            "power": round(reading.power, 1),
            "soc": round(reading.soc, 1),
            "timestamp": int(time.time()),
        }
        if reading.consumed_ah is not None:
            data["consumed_ah"] = round(reading.consumed_ah, 1)
        if reading.time_remaining is not None:
            data["time_remaining_mins"] = reading.time_remaining

        self.client.publish(f"{prefix}/state", json.dumps(data))
