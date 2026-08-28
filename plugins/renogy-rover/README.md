# Renogy Rover MPPT BLE Monitor

Read data from Renogy Rover MPPT solar charge controllers via Bluetooth (BT-2 module).

## Requirements

- Linux with BlueZ Bluetooth stack
- Renogy MPPT controller with BT-2 module installed
- Python 3.9+

## Installation

### Quick Start (CLI only)

```bash
cd /path/to/edge_server
make renogy-setup
make renogy-scan
renogy-rover read --address XX:XX:XX:XX:XX:XX
```

### System Service

```bash
sudo ./install.sh
sudo nano /etc/renogy-rover/config.yaml  # Add your device address
sudo systemctl restart renogy-rover
```

## Configuration

Config file: `/etc/renogy-rover/config.yaml` or `~/.config/renogy-rover.yaml`

```yaml
address: "XX:XX:XX:XX:XX:XX"  # BT-2 module MAC address
device_id: 255                 # Modbus device ID (default 255)
poll_interval: 30              # Seconds between readings

mqtt:
  host: localhost
  port: 1883
  topic_prefix: renogy/rover
```

Environment variables override config file:
- `RENOGY_ADDRESS` - Device MAC address
- `RENOGY_DEVICE_ID` - Modbus device ID
- `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`

## CLI Commands

```bash
renogy-rover check           # Verify Bluetooth is working
renogy-rover scan            # Find BT-2 modules
renogy-rover read            # Single reading from configured device
renogy-rover read -c         # Continuous readings
renogy-rover config --show   # Show current config
renogy-rover service         # Run as service (for systemd)
```

## MQTT Topics

When MQTT is enabled, publishes to:
- `renogy/rover/battery_voltage` - Battery voltage (V)
- `renogy/rover/battery_current` - Battery current (A)
- `renogy/rover/battery_soc` - State of charge (%)
- `renogy/rover/pv_voltage` - Solar panel voltage (V)
- `renogy/rover/pv_power` - Solar power (W)
- `renogy/rover/charge_state` - Charging state
- `renogy/rover/state` - Combined JSON payload

Home Assistant auto-discovery is published to `homeassistant/sensor/renogy_rover/*/config`.

## Compatible Controllers

- Rover series (20A, 30A, 40A, 60A)
- Wanderer series
- Adventurer series
- Most Renogy controllers with BT-2 module support

## Troubleshooting

### Can't find device

1. Ensure BT-2 module is plugged in and LED is blinking
2. Pair device with Renogy DC Home app first
3. Run `renogy-rover scan --all` to see all BLE devices
4. Check Bluetooth: `hciconfig -a`

### Connection timeouts

- Move closer to the controller
- Check for Bluetooth interference
- Try increasing poll interval
