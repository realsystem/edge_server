#!/bin/bash
# Uninstall NUT power monitor

set -euo pipefail

echo "=== Uninstalling NUT Power Monitor ==="

# Stop services
echo "Stopping services..."
systemctl stop nut-monitor.service 2>/dev/null || true
systemctl stop nut-server.service 2>/dev/null || true
systemctl stop nut-driver.service 2>/dev/null || true
systemctl stop battery-poller.service 2>/dev/null || true

systemctl disable nut-monitor.service 2>/dev/null || true
systemctl disable nut-server.service 2>/dev/null || true
systemctl disable battery-poller.service 2>/dev/null || true

# Remove battery poller
rm -f /usr/local/bin/battery-poller.sh
rm -f /etc/systemd/system/battery-poller.service

# Restore original NUT config if exists
if [ -f /etc/nut/nut.conf.orig ]; then
    echo "Restoring original NUT config..."
    mv /etc/nut/nut.conf.orig /etc/nut/nut.conf
    mv /etc/nut/ups.conf.orig /etc/nut/ups.conf 2>/dev/null || true
    mv /etc/nut/upsd.conf.orig /etc/nut/upsd.conf 2>/dev/null || true
    mv /etc/nut/upsd.users.orig /etc/nut/upsd.users 2>/dev/null || true
    mv /etc/nut/upsmon.conf.orig /etc/nut/upsmon.conf 2>/dev/null || true
fi

# Clean up runtime files
rm -f /var/run/nut/laptop.dev

systemctl daemon-reload

echo ""
echo "NUT Power Monitor uninstalled."
echo "NUT packages are still installed. To fully remove:"
echo "  apt remove nut nut-client nut-server"
