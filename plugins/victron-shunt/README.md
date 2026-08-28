# Victron Smart Shunt BLE Reader

CLI tool to read battery data from Victron Smart Shunt via Bluetooth Low Energy.

## Quick Start

```bash
# From project root - use Makefile (recommended)
make victron-setup   # Install into project venv
make victron-check   # Check Bluetooth
make victron-scan    # Scan for devices

# Read data (get key from Victron Connect app)
.venv/bin/victron-shunt read --address AA:BB:CC:DD:EE:FF --key <YOUR_KEY>
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
Verifies Bluetooth adapter is available and working.

### Scan for Devices
```bash
# Find Victron devices
victron-shunt scan

# Find all BLE devices (debugging)
victron-shunt scan --all
```

### Read Data
```bash
# Single reading
victron-shunt read -a AA:BB:CC:DD:EE:FF -k <encryption_key>

# Continuous mode
victron-shunt read -a AA:BB:CC:DD:EE:FF -k <encryption_key> --continuous
```

### Get Encryption Key
```bash
victron-shunt info
```

## Getting the Encryption Key

1. Open **Victron Connect** app on your phone
2. Connect to your Smart Shunt
3. Tap the **gear icon** (Settings)
4. Scroll to **Product Info**
5. Find **Encryption data** - tap to reveal
6. Copy the 32-character hex key

## Ubuntu Setup

On Ubuntu, ensure BlueZ is installed and running:

```bash
# Install BlueZ
sudo apt install bluez

# Start Bluetooth service
sudo systemctl start bluetooth
sudo systemctl enable bluetooth

# Check adapter
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
