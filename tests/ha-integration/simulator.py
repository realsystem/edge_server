#!/usr/bin/env python3
"""Simulate Victron and Renogy MQTT messages for HA testing."""

import json
import os
import random
import time

import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "admin")
MQTT_PASS = os.environ.get("MQTT_PASS", "testpass123")


def publish_discovery(client):
    """Publish Home Assistant MQTT discovery configs."""

    # Victron Shunt device
    victron_device = {
        "identifiers": ["victron_shunt"],
        "name": "Victron Smart Shunt",
        "manufacturer": "Victron Energy",
        "model": "SmartShunt 500A",
    }

    victron_sensors = [
        ("voltage", "V", "voltage", "measurement", "mdi:flash"),
        ("current", "A", "current", "measurement", "mdi:current-dc"),
        ("power", "W", "power", "measurement", "mdi:lightning-bolt"),
        ("soc", "%", "battery", "measurement", "mdi:battery"),
    ]

    for name, unit, device_class, state_class, icon in victron_sensors:
        config = {
            "name": name.replace("_", " ").title(),
            "unique_id": f"victron_shunt_{name}",
            "state_topic": f"victron/shunt/{name}",
            "unit_of_measurement": unit,
            "device_class": device_class,
            "state_class": state_class,
            "icon": icon,
            "device": victron_device,
        }
        client.publish(
            f"homeassistant/sensor/victron_shunt/{name}/config",
            json.dumps(config),
            retain=True,
        )

    # Renogy Rover device
    renogy_device = {
        "identifiers": ["renogy_rover"],
        "name": "Renogy Rover MPPT",
        "manufacturer": "Renogy",
        "model": "Rover 40A MPPT",
    }

    renogy_sensors = [
        ("battery_voltage", "V", "voltage", "measurement", "mdi:flash"),
        ("battery_soc", "%", "battery", "measurement", "mdi:battery"),
        ("pv_voltage", "V", "voltage", "measurement", "mdi:solar-panel"),
        ("pv_power", "W", "power", "measurement", "mdi:solar-power"),
        ("charging_current", "A", "current", "measurement", "mdi:current-dc"),
    ]

    for name, unit, device_class, state_class, icon in renogy_sensors:
        config = {
            "name": name.replace("_", " ").title(),
            "unique_id": f"renogy_rover_{name}",
            "state_topic": f"renogy/rover/{name}",
            "unit_of_measurement": unit,
            "device_class": device_class,
            "state_class": state_class,
            "icon": icon,
            "device": renogy_device,
        }
        client.publish(
            f"homeassistant/sensor/renogy_rover/{name}/config",
            json.dumps(config),
            retain=True,
        )

    print("Published HA discovery configs")


def publish_readings(client):
    """Publish simulated sensor readings."""

    # Victron Shunt readings
    voltage = 12.8 + random.uniform(-0.3, 0.3)
    current = random.uniform(-5, 15)  # Negative = discharge
    power = voltage * current
    soc = max(0, min(100, 75 + random.uniform(-5, 5)))

    client.publish("victron/shunt/voltage", f"{voltage:.2f}")
    client.publish("victron/shunt/current", f"{current:.2f}")
    client.publish("victron/shunt/power", f"{power:.1f}")
    client.publish("victron/shunt/soc", f"{soc:.1f}")

    # Renogy Rover readings
    bat_voltage = 13.2 + random.uniform(-0.2, 0.2)
    bat_soc = max(0, min(100, 80 + random.uniform(-3, 3)))
    pv_voltage = 18.5 + random.uniform(-1, 1)
    pv_power = random.uniform(50, 120)
    charging_current = pv_power / bat_voltage

    client.publish("renogy/rover/battery_voltage", f"{bat_voltage:.2f}")
    client.publish("renogy/rover/battery_soc", f"{bat_soc:.0f}")
    client.publish("renogy/rover/pv_voltage", f"{pv_voltage:.2f}")
    client.publish("renogy/rover/pv_power", f"{pv_power:.1f}")
    client.publish("renogy/rover/charging_current", f"{charging_current:.2f}")

    print(
        f"Victron: {voltage:.1f}V {current:+.1f}A {soc:.0f}% | "
        f"Renogy: {bat_voltage:.1f}V {pv_power:.0f}W"
    )


def main():
    print(f"Connecting to MQTT broker {MQTT_HOST}:{MQTT_PORT}...")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASS)

    # Retry connection
    for attempt in range(30):
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            break
        except Exception as e:
            print(f"Connection attempt {attempt + 1} failed: {e}")
            time.sleep(2)
    else:
        print("Failed to connect to MQTT broker")
        return

    client.loop_start()
    print("Connected to MQTT broker")

    # Publish discovery configs
    publish_discovery(client)

    # Publish readings every 5 seconds
    try:
        while True:
            publish_readings(client)
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
