#!/usr/bin/env bash
#===============================================================================
# Find ONVIF cameras on the network and add new ones to Frigate.
#
# Usage:
#   sudo ./discover-cameras.sh                 # add new cameras, restart Frigate
#   sudo ./discover-cameras.sh --dry-run       # only show what would be added
#   sudo ./discover-cameras.sh --host 192.168.1.50 [--host ...]
#   sudo ./discover-cameras.sh --verbose       # trace every step (debugging)
#
# Camera logins are read from .cam (one user:pass per line), looked up in:
#   $CAM_CREDENTIALS_FILE, ./.cam next to this script, $EDGE_SERVER_DIR/.cam
#===============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${EDGE_SERVER_DIR:-/opt/edge-server}"
CONFIG="$APP_DIR/frigate/config.yml"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo (the Frigate config is readable by root only)" >&2
    exit 1
fi

apply=(--apply)
args=()
for arg in "$@"; do
    case "$arg" in
        --dry-run) apply=() ;;
        -h|--help)
            sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) args+=("$arg") ;;
    esac
done

cred_file=""
for candidate in "${CAM_CREDENTIALS_FILE:-}" "$SCRIPT_DIR/.cam" "$APP_DIR/.cam"; do
    if [[ -n "$candidate" && -f "$candidate" ]]; then
        cred_file="$candidate"
        break
    fi
done
if [[ -z "$cred_file" ]]; then
    echo "No .cam file found. Create $SCRIPT_DIR/.cam with one user:pass per line" >&2
    echo "(and chmod 600 it), then run this again." >&2
    exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
    echo "Frigate config not found: $CONFIG (run deploy-edge-server.sh first)" >&2
    exit 1
fi

before=$(sha256sum "$CONFIG" | cut -d' ' -f1)
python3 "$SCRIPT_DIR/tools/camera_discovery.py" \
    --credentials "$cred_file" --config "$CONFIG" "${apply[@]}" "${args[@]}"
after=$(sha256sum "$CONFIG" | cut -d' ' -f1)

if [[ "$before" != "$after" ]]; then
    echo "Restarting Frigate to load the new cameras..."
    (cd "$APP_DIR" && docker compose restart frigate)
    echo "Done. Open Frigate at http://$(hostname -I | awk '{print $1}'):5000"
fi
