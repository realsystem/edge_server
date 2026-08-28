"""MQTT publishing for Renogy Rover readings."""

import json
import time
from typing import Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from .config import MQTTConfig
from .reader import RoverReading


class MQTTPublisher:
    """Publish solar charger readings to MQTT broker."""

    def __init__(self, config: MQTTConfig, device_name: str = "rover"):
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

        device_id = f"renogy_{self.device_name}"
        device_info = {
            "identifiers": [device_id],
            "name": f"Renogy {self.device_name.replace('_', ' ').title()}",
            "manufacturer": "Renogy",
            "model": "Rover MPPT",
        }

        sensors = [
            # Battery
            ("battery_voltage", "V", "voltage", "measurement", "mdi:flash"),
            ("battery_current", "A", "current", "measurement", "mdi:current-dc"),
            ("battery_soc", "%", "battery", "measurement", "mdi:battery"),
            # Solar
            ("pv_voltage", "V", "voltage", "measurement", "mdi:solar-panel"),
            ("pv_current", "A", "current", "measurement", "mdi:solar-power"),
            ("pv_power", "W", "power", "measurement", "mdi:solar-power-variant"),
            # Load
            ("load_power", "W", "power", "measurement", "mdi:power-plug"),
            # Stats
            ("daily_energy", "Wh", "energy", "total_increasing", "mdi:lightning-bolt"),
            # Controller
            ("controller_temp", "°C", "temperature", "measurement", "mdi:thermometer"),
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

        # Charge state as text sensor
        config_topic = f"homeassistant/sensor/{device_id}/charge_state/config"
        payload = {
            "name": "Charge State",
            "unique_id": f"{device_id}_charge_state",
            "state_topic": f"{self.config.topic_prefix}/charge_state",
            "icon": "mdi:battery-charging",
            "device": device_info,
        }
        self.client.publish(config_topic, json.dumps(payload), retain=True)

    def publish_reading(self, reading: RoverReading) -> None:
        """Publish a solar charger reading to MQTT."""
        if not self.client or not self._connected:
            return

        prefix = self.config.topic_prefix

        # Battery
        self.client.publish(f"{prefix}/battery_voltage", f"{reading.battery_voltage:.2f}")
        self.client.publish(f"{prefix}/battery_current", f"{reading.battery_current:.2f}")
        self.client.publish(f"{prefix}/battery_soc", f"{reading.battery_soc}")

        # Solar
        self.client.publish(f"{prefix}/pv_voltage", f"{reading.pv_voltage:.2f}")
        self.client.publish(f"{prefix}/pv_current", f"{reading.pv_current:.2f}")
        self.client.publish(f"{prefix}/pv_power", f"{reading.pv_power:.1f}")

        # Load
        self.client.publish(f"{prefix}/load_power", f"{reading.load_power:.1f}")

        # State
        self.client.publish(f"{prefix}/charge_state", reading.charge_state)

        # Stats
        self.client.publish(f"{prefix}/daily_energy", f"{reading.daily_energy:.1f}")

        # Temperature
        if reading.controller_temp is not None:
            self.client.publish(f"{prefix}/controller_temp", f"{reading.controller_temp}")

        # Combined JSON
        data = {
            "battery_voltage": round(reading.battery_voltage, 2),
            "battery_current": round(reading.battery_current, 2),
            "battery_soc": reading.battery_soc,
            "pv_voltage": round(reading.pv_voltage, 2),
            "pv_power": round(reading.pv_power, 1),
            "charge_state": reading.charge_state,
            "timestamp": int(time.time()),
        }
        self.client.publish(f"{prefix}/state", json.dumps(data))
