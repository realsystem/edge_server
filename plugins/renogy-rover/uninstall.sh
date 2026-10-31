#!/bin/bash
# Uninstall the Renogy Rover monitor.
#
# Removes the service and /opt/renogy-rover. The configuration (which holds the
# device key and MQTT password) is kept unless REMOVE_CONFIG=true, so a later
# reinstall can reuse it. Shared packages (bluez, python3) stay installed.

set -euo pipefail

SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
OPT_DIR="${OPT_DIR:-/opt}"
ETC_DIR="${ETC_DIR:-/etc}"
REMOVE_CONFIG="${REMOVE_CONFIG:-false}"

echo "=== Uninstalling Renogy Rover ==="

systemctl stop renogy-rover.service 2>/dev/null || true
systemctl disable renogy-rover.service 2>/dev/null || true

rm -f "$SYSTEMD_DIR/renogy-rover.service"
rm -rf "${OPT_DIR:?}/renogy-rover"

if [[ "$REMOVE_CONFIG" == "true" ]]; then
    rm -rf "${ETC_DIR:?}/renogy-rover"
    echo "Configuration removed."
else
    echo "Configuration kept in $ETC_DIR/renogy-rover (REMOVE_CONFIG=true deletes it)."
fi

systemctl daemon-reload 2>/dev/null || true

echo ""
echo "Renogy Rover uninstalled."
echo "Bluetooth packages are still installed (other things may use them)."
