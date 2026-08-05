#!/bin/bash
# Setup test target container for bootstrap testing

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Setting up test target ==="

# Clean up any existing containers from previous runs
echo "Cleaning up previous containers..."
docker rm -f frigate homeassistant mosquitto 2>/dev/null || true

# Create shared directories on host for edge-server deployment
echo "Creating shared directories..."
sudo rm -rf /tmp/edge-server-test /tmp/edge-server-test-storage 2>/dev/null || true
mkdir -p /tmp/edge-server-test /tmp/edge-server-test-storage
chmod 777 /tmp/edge-server-test /tmp/edge-server-test-storage

# Generate SSH key if not exists
if [[ ! -f ssh_keys/id_ed25519 ]]; then
    echo "Generating SSH key..."
    mkdir -p ssh_keys
    ssh-keygen -t ed25519 -f ssh_keys/id_ed25519 -N "" -C "test-target"
    cat ssh_keys/id_ed25519.pub > ssh_keys/authorized_keys
    chmod 600 ssh_keys/authorized_keys
fi

# Build and start container
echo "Building and starting container..."
docker compose up -d --build

# Wait for SSH
echo "Waiting for SSH to be ready..."
for i in {1..30}; do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=2 -i ssh_keys/id_ed25519 -p 2222 testuser@localhost true 2>/dev/null; then
        echo "SSH ready!"
        break
    fi
    sleep 1
done

echo ""
echo "=== Test target ready ==="
echo ""
echo "Connect with:"
echo "  ssh -i $SCRIPT_DIR/ssh_keys/id_ed25519 -p 2222 testuser@localhost"
echo ""
echo "Test bootstrap (full deployment, auto mode):"
echo "  SECRETS_PASSWORD=test MQTT_PASS=testmqtt REOLINK_PASS=testcam \\"
echo "    ./bootstrap.sh --config tests/target/bootstrap.cfg --auto localhost"
echo ""
echo "NOTE: Uses host Docker socket - containers run on your Mac."
echo "      Clean up containers: docker stop frigate homeassistant mosquitto"
echo ""
echo "Stop with:"
echo "  cd $SCRIPT_DIR && docker compose down"
