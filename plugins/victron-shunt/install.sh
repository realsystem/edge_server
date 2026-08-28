#!/bin/bash
# Install Victron Smart Shunt BLE monitor
# Called from deploy-edge-server.sh with:
#   VICTRON_ADDRESS=XX:XX:XX:XX:XX:XX VICTRON_KEY=... ./install.sh

set -e

INSTALL_DIR="/opt/victron-shunt"
CONFIG_DIR="/etc/victron-shunt"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Installing Victron Smart Shunt Monitor ==="

# Check requirements
if [[ -z "$VICTRON_ADDRESS" ]] || [[ -z "$VICTRON_KEY" ]]; then
    echo "ERROR: VICTRON_ADDRESS and VICTRON_KEY must be set"
    exit 1
fi

# Install system dependencies
echo "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip bluez bluetooth

# Create install directory
echo "Creating install directory..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$CONFIG_DIR"

# Copy source
cp -r "$SCRIPT_DIR/src" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/pyproject.toml" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

# Create virtual environment
echo "Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -e "$INSTALL_DIR" -q
"$INSTALL_DIR/venv/bin/pip" install paho-mqtt -q

# Create config file
echo "Creating config file..."
cat > "$CONFIG_DIR/config.yaml" << EOF
address: "${VICTRON_ADDRESS}"
key: "${VICTRON_KEY}"
mqtt:
  host: localhost
  port: 1883
  user: ${MQTT_USER:-}
  topic_prefix: victron/smartshunt
EOF
chmod 600 "$CONFIG_DIR/config.yaml"

# Install systemd service
echo "Installing systemd service..."
cp "$SCRIPT_DIR/victron-shunt.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable victron-shunt

# Start bluetooth if not running
systemctl start bluetooth || true

# Start service
echo "Starting service..."
systemctl start victron-shunt

# Check status
sleep 2
if systemctl is-active --quiet victron-shunt; then
    echo ""
    echo "=== Victron Smart Shunt Monitor installed successfully ==="
    echo ""
    echo "Service status: $(systemctl is-active victron-shunt)"
    echo "View logs: journalctl -u victron-shunt -f"
    echo "MQTT topics: victron/smartshunt/voltage, current, soc, power"
else
    echo ""
    echo "WARNING: Service failed to start. Check logs:"
    echo "  journalctl -u victron-shunt -n 50"
fi
