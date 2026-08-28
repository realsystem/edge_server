# Victron Smart Shunt BLE Reader

CLI tool to read battery data from Victron Smart Shunt via Bluetooth Low Energy.
Publishes to MQTT for Home Assistant integration.

## Quick Start

```bash
# From project root - use Makefile (recommended)
make victron-setup   # Install into project venv
make victron-check   # Check Bluetooth
make victron-scan    # Scan for devices

# Save config (one time)
.venv/bin/victron-shunt config --address AA:BB:CC:DD:EE:FF --key <YOUR_KEY>

# Read data
.venv/bin/victron-shunt read

# Read with MQTT publishing
.venv/bin/victron-shunt read --mqtt --continuous
```

### Manual install (standalone venv)

```bash
cd plugins/victron-shunt
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
victron-shunt check
```

## Commands

### Check Bluetooth
```bash
victron-shunt check
```

### Scan for Devices
```bash
victron-shunt scan          # Find Victron devices
victron-shunt scan --all    # Find all BLE devices
```

### Configure
```bash
# Save device config
victron-shunt config --address AA:BB:CC:DD:EE:FF --key <key>

# Configure MQTT
victron-shunt config --mqtt-host localhost --mqtt-user edge

# Show current config
victron-shunt config --show
```

### Read Data
```bash
# Single reading
victron-shunt read

# Continuous mode
victron-shunt read --continuous

# With MQTT publishing
victron-shunt read --mqtt --continuous
```

### Run as Service
```bash
# Runs continuously with MQTT, designed for systemd
victron-shunt service
```

## Configuration

Configuration is loaded in order of precedence (highest first):
1. **CLI arguments** (`--address`, `--key`)
2. **Environment variables** (`VICTRON_ADDRESS`, `VICTRON_KEY`, etc.)
3. **Config file** (first found is used)

Config file locations searched:
- `~/.config/victron-shunt.yaml` (user config, created by `config` command)
- `/etc/victron-shunt/config.yaml` (system config, used by service)

### Creating config

```bash
# Interactive - saves to ~/.config/victron-shunt.yaml
victron-shunt config --address AA:BB:CC:DD:EE:FF --key <key>

# With MQTT settings
victron-shunt config --address AA:BB:CC:DD:EE:FF --key <key> \
    --mqtt-host localhost --mqtt-user edge

# View current config
victron-shunt config --show
```

### Config file format

```yaml
address: "AA:BB:CC:DD:EE:FF"
key: "0df4d0395b7d1a876c0c33ecb9e70dcd"
mqtt:
  host: localhost
  port: 1883
  user: edge
  topic_prefix: victron/smartshunt
```

### Environment variables

Override any config file setting:
- `VICTRON_ADDRESS` - Device MAC address
- `VICTRON_KEY` - Encryption key
- `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`

Useful for systemd service with `EnvironmentFile`.

## Getting the Encryption Key

1. Open **Victron Connect** app on your phone
2. Connect to your Smart Shunt
3. Tap the **gear icon** (Settings)
4. Scroll to **Product Info**
5. Find **Encryption data** - tap to reveal
6. Copy the 32-character hex key

## MQTT Topics

When running with `--mqtt` or as a service:

| Topic | Description |
|-------|-------------|
| `victron/smartshunt/voltage` | Battery voltage (V) |
| `victron/smartshunt/current` | Current (A, positive=charging) |
| `victron/smartshunt/power` | Power (W) |
| `victron/smartshunt/soc` | State of charge (%) |
| `victron/smartshunt/consumed_ah` | Consumed Ah |
| `victron/smartshunt/state` | Combined JSON |

Home Assistant auto-discovery is published on connect.

## Systemd Service

For production deployment, install via deploy script or manually:

```bash
sudo cp victron-shunt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable victron-shunt
sudo systemctl start victron-shunt

# View logs
journalctl -u victron-shunt -f
```

## Ubuntu Setup

Ensure BlueZ is installed:

```bash
sudo apt install bluez
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
hciconfig -a
```

## Output Example

```
Battery Status:
  Voltage:     12.85 V
  Current:     -2.30 A
  Power:       -29.6 W
  State of Charge: 87.5%
  Consumed:    15.2 Ah
  Time Left:   6h 30m
```
