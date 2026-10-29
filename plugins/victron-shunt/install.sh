#!/bin/bash
# Install Victron Smart Shunt BLE monitor
# Called from deploy-edge-server.sh with:
#   VICTRON_ADDRESS=XX:XX:XX:XX:XX:XX VICTRON_KEY=... ./install.sh

set -euo pipefail

VICTRON_ADDRESS="${VICTRON_ADDRESS:-}"
VICTRON_KEY="${VICTRON_KEY:-}"
MQTT_USER="${MQTT_USER:-admin}"
MQTT_PASS="${MQTT_PASS:-}"

# YAML double-quoted scalar
yaml_quote() {
    local v="${1//\\/\\\\}"
    v="${v//\"/\\\"}"
    printf '"%s"' "$v"
}

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
"$INSTALL_DIR/venv/bin/pip" install "$INSTALL_DIR" -q

# Create config file (only if not exists, to preserve user changes)
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
    echo "Creating config file..."
    (umask 077 && cat > "$CONFIG_DIR/config.yaml" << EOF
address: $(yaml_quote "$VICTRON_ADDRESS")
key: $(yaml_quote "$VICTRON_KEY")
mqtt:
  host: localhost
  port: 1883
  user: $(yaml_quote "$MQTT_USER")
  password: $(yaml_quote "$MQTT_PASS")
  topic_prefix: victron/smartshunt
EOF
    )
    chmod 600 "$CONFIG_DIR/config.yaml"
else
    echo "Config file exists, preserving..."
fi

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
