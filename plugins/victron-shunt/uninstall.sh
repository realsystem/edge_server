#!/bin/bash
# Uninstall the Victron Smart Shunt monitor.
#
# Removes the service and /opt/victron-shunt. The configuration (which holds the
# device key and MQTT password) is kept unless REMOVE_CONFIG=true, so a later
# reinstall can reuse it. Shared packages (bluez, python3) stay installed.

set -euo pipefail

SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
OPT_DIR="${OPT_DIR:-/opt}"
ETC_DIR="${ETC_DIR:-/etc}"
REMOVE_CONFIG="${REMOVE_CONFIG:-false}"

echo "=== Uninstalling Victron Smart Shunt ==="

systemctl stop victron-shunt.service 2>/dev/null || true
systemctl disable victron-shunt.service 2>/dev/null || true

rm -f "$SYSTEMD_DIR/victron-shunt.service"
rm -rf "${OPT_DIR:?}/victron-shunt"

if [[ "$REMOVE_CONFIG" == "true" ]]; then
    rm -rf "${ETC_DIR:?}/victron-shunt"
    echo "Configuration removed."
else
    echo "Configuration kept in $ETC_DIR/victron-shunt (REMOVE_CONFIG=true deletes it)."
fi

systemctl daemon-reload 2>/dev/null || true

echo ""
echo "Victron Smart Shunt uninstalled."
echo "Bluetooth packages are still installed (other things may use them)."
