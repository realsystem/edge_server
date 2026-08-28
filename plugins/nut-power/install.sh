#!/bin/bash
# Install NUT power monitor for laptop battery
# Idempotent - safe to run multiple times

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SHUTDOWN_DELAY="${NUT_SHUTDOWN_DELAY:-600}"
LOW_BATTERY="${NUT_LOW_BATTERY:-10}"

echo "=== Installing NUT Power Monitor ==="

# Check if running on a laptop (has battery)
if [ ! -d /sys/class/power_supply/BAT0 ] && [ ! -d /sys/class/power_supply/BAT1 ]; then
    echo "WARNING: No battery detected. This appears to be a desktop."
    echo "NUT power monitor is designed for laptops with batteries."
    read -rp "Continue anyway? [y/N]: " response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        echo "Skipping NUT installation."
        exit 0
    fi
fi

# Install NUT packages (idempotent)
echo "Installing NUT packages..."
apt-get update -qq
apt-get install -y -qq nut nut-client nut-server

# Stop services before configuring
echo "Stopping NUT services..."
systemctl stop nut-monitor.service 2>/dev/null || true
systemctl stop nut-server.service 2>/dev/null || true
systemctl stop nut-driver.service 2>/dev/null || true
systemctl stop battery-poller.service 2>/dev/null || true

# Create runtime directory
mkdir -p /var/run/nut
chown nut:nut /var/run/nut

# Install battery poller script
echo "Installing battery poller..."
cp "$SCRIPT_DIR/battery-poller.sh" /usr/local/bin/
chmod +x /usr/local/bin/battery-poller.sh

# Install battery poller service
cp "$SCRIPT_DIR/battery-poller.service" /etc/systemd/system/

# Update poller service with configured values
sed -i "s/NUT_LOW_BATTERY=10/NUT_LOW_BATTERY=$LOW_BATTERY/" /etc/systemd/system/battery-poller.service

# Backup existing NUT config (only first time)
if [ -f /etc/nut/nut.conf ] && [ ! -f /etc/nut/nut.conf.orig ]; then
    echo "Backing up original NUT config..."
    cp /etc/nut/nut.conf /etc/nut/nut.conf.orig
    cp /etc/nut/ups.conf /etc/nut/ups.conf.orig 2>/dev/null || true
    cp /etc/nut/upsd.conf /etc/nut/upsd.conf.orig 2>/dev/null || true
    cp /etc/nut/upsd.users /etc/nut/upsd.users.orig 2>/dev/null || true
    cp /etc/nut/upsmon.conf /etc/nut/upsmon.conf.orig 2>/dev/null || true
fi

# Install NUT configuration
echo "Configuring NUT..."
cp "$SCRIPT_DIR/config/nut.conf" /etc/nut/
cp "$SCRIPT_DIR/config/ups.conf" /etc/nut/
cp "$SCRIPT_DIR/config/upsd.conf" /etc/nut/
cp "$SCRIPT_DIR/config/upsd.users" /etc/nut/
cp "$SCRIPT_DIR/config/upsmon.conf" /etc/nut/

# Update shutdown delay in upsmon.conf
sed -i "s/ONBATTERYDELAY 600/ONBATTERYDELAY $SHUTDOWN_DELAY/" /etc/nut/upsmon.conf

# Set proper permissions
chown root:nut /etc/nut/*.conf
chmod 640 /etc/nut/*.conf
chown root:nut /etc/nut/upsd.users
chmod 640 /etc/nut/upsd.users

# Reload systemd
systemctl daemon-reload

# Start battery poller first (creates the dummy file)
echo "Starting battery poller..."
systemctl enable battery-poller.service
systemctl start battery-poller.service

# Wait for dummy file to be created
echo "Waiting for battery status..."
for i in {1..10}; do
    if [ -f /var/run/nut/laptop.dev ]; then
        break
    fi
    sleep 1
done

# Start NUT services
echo "Starting NUT services..."
systemctl enable nut-server.service
systemctl start nut-server.service
sleep 2

systemctl enable nut-monitor.service
systemctl start nut-monitor.service

# Verify installation
echo ""
echo "=== NUT Power Monitor Installed ==="
echo ""
echo "Configuration:"
echo "  Shutdown delay:  $((SHUTDOWN_DELAY / 60)) minutes on battery"
echo "  Low battery:     ${LOW_BATTERY}%"
echo ""
echo "Status commands:"
echo "  upsc laptop              - Show battery status"
echo "  systemctl status nut-monitor"
echo "  journalctl -u nut-monitor -f"
echo ""

# Show current status
echo "Current battery status:"
upsc laptop 2>/dev/null || echo "  (waiting for first poll...)"
