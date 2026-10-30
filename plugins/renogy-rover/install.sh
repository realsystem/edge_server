#!/bin/bash
# Install Renogy Rover BLE monitor
# Called from deploy-edge-server.sh with RENOGY_ADDRESS, MQTT_USER, MQTT_PASS
set -euo pipefail

RENOGY_ADDRESS="${RENOGY_ADDRESS:-}"
MQTT_USER="${MQTT_USER:-}"
MQTT_PASS="${MQTT_PASS:-}"

# YAML double-quoted scalar
yaml_quote() {
    local v="${1//\\/\\\\}"
    v="${v//\"/\\\"}"
    printf '"%s"' "$v"
}

INSTALL_DIR="/opt/renogy-rover"
SERVICE_NAME="renogy-rover"

echo "Installing Renogy Rover BLE monitor..."

# Check for root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo ./install.sh)"
    exit 1
fi

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Install system dependencies
echo "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip bluetooth bluez

# Create install directory
echo "Setting up ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"

# Copy source files
cp -r "${SCRIPT_DIR}/src" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/pyproject.toml" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"

# Create virtual environment if it doesn't exist
if [ ! -d "${INSTALL_DIR}/venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "${INSTALL_DIR}/venv"
fi

# Install dependencies
echo "Installing Python dependencies..."
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
"${INSTALL_DIR}/venv/bin/pip" install --quiet "${INSTALL_DIR}"

# Install systemd service
echo "Installing systemd service..."
cp "${SCRIPT_DIR}/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

# Create config directory
mkdir -p /etc/renogy-rover
chmod 700 /etc/renogy-rover

# Create config if none exists (uses values passed in by the deploy script so
# the service can actually reach the authenticated MQTT broker)
if [ ! -f /etc/renogy-rover/config.yaml ]; then
    mqtt_auth="  # user: \"\"
  # password: \"\""
    if [ -n "$MQTT_USER" ]; then
        mqtt_auth="  user: $(yaml_quote "$MQTT_USER")
  password: $(yaml_quote "$MQTT_PASS")"
    fi
    (umask 077 && cat > /etc/renogy-rover/config.yaml << EOF
# Renogy Rover MPPT BLE configuration
# Find your device address with: renogy-rover scan

address: $(yaml_quote "$RENOGY_ADDRESS")  # Device MAC address (required)
device_id: 255  # Modbus device ID (usually 255)
poll_interval: 30  # Seconds between readings

mqtt:
  host: localhost
  port: 1883
${mqtt_auth}
  topic_prefix: renogy/rover
EOF
    )
    echo "Created /etc/renogy-rover/config.yaml"
fi
chmod 600 /etc/renogy-rover/config.yaml

# Enable and start service
echo "Enabling and starting service..."
systemctl enable "${SERVICE_NAME}"
systemctl start "${SERVICE_NAME}" || true

echo ""
echo "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Find your device: ${INSTALL_DIR}/venv/bin/renogy-rover scan"
echo "  2. Edit config: sudo nano /etc/renogy-rover/config.yaml"
echo "  3. Restart service: sudo systemctl restart ${SERVICE_NAME}"
echo "  4. Check status: sudo systemctl status ${SERVICE_NAME}"
echo "  5. View logs: sudo journalctl -u ${SERVICE_NAME} -f"
