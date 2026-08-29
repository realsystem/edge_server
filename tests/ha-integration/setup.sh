#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Setting up HA integration test environment..."

# Create mosquitto password file
echo "Creating MQTT credentials (admin/testpass123)..."
docker run --rm -v "$SCRIPT_DIR/mosquitto/config:/mosquitto/config" \
    eclipse-mosquitto:2 mosquitto_passwd -b -c /mosquitto/config/passwd admin testpass123

# Fix permissions for mosquitto
chmod 600 mosquitto/config/passwd

# Create minimal HA config
cat > ha-config/configuration.yaml << 'EOF'
homeassistant:
  name: Test Home
  unit_system: metric
  time_zone: America/Los_Angeles

# Enable default integrations
default_config:

# Logger for debugging
logger:
  default: info
  logs:
    homeassistant.components.mqtt: debug
EOF

echo "Starting containers..."
docker compose up -d

echo ""
echo "Waiting for services to start..."
sleep 10

echo ""
echo "=== Test Environment Ready ==="
echo ""
echo "Home Assistant: http://localhost:8123"
echo "  (First run: create account, then add MQTT integration)"
echo ""
echo "MQTT Broker: localhost:1883"
echo "  User: admin"
echo "  Pass: testpass123"
echo ""
echo "To add MQTT in HA:"
echo "  1. Settings -> Devices & Services -> + Add Integration"
echo "  2. Search 'MQTT'"
echo "  3. Broker: mosquitto, Port: 1883, User: admin, Pass: testpass123"
echo ""
echo "View simulator logs: docker logs -f mqtt-simulator"
echo "Stop: docker compose down"
