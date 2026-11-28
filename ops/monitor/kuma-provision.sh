#!/usr/bin/env bash
# Sets up an Uptime Kuma server from code (database, admin account, Telegram notification, push monitors).
# It runs tools/kuma_provision.py, which needs the Socket.IO client library; this wrapper keeps that library in a private
# virtual environment so nothing is installed system-wide. All options are passed on: see --help.
#
#   ./ops/monitor/kuma-provision.sh --url http://127.0.0.1:3001 --public-url https://monitor.example.com \
#       --admin-password-file /root/.kuma-admin --telegram-token-file /root/.kuma-telegram-token \
#       --telegram-chat-id-file /root/.kuma-telegram-chat --monitor "Home HA heartbeat" --urls-out /root/heartbeat-urls.env
#
# Add --plan first to see what would change. Run it on the VM against Kuma's loopback address (--url) because the admin
# page of a public monitor is normally limited to your own address; --public-url only builds the push URLs.
# The virtual environment lives in $KUMA_PROVISION_VENV (default ~/.cache/edge-monitor-venv).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/../../tools/kuma_provision.py"
VENV="${KUMA_PROVISION_VENV:-$HOME/.cache/edge-monitor-venv}"

[ -f "$TOOL" ] || { echo "ERROR: $TOOL is missing (run this from a full clone of the repository)" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is not installed" >&2; echo "    hint: sudo apt-get install -y python3 python3-venv" >&2; exit 1; }

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating $VENV"
  python3 -m venv "$VENV" || {
    echo "ERROR: could not create a virtual environment" >&2
    echo "    hint: sudo apt-get install -y python3-venv" >&2
    exit 1
  }
fi
if ! "$VENV/bin/python" -c 'import socketio, websocket' >/dev/null 2>&1; then
  echo "==> installing the Socket.IO client into $VENV"
  "$VENV/bin/python" -m pip install -q --disable-pip-version-check 'python-socketio[client]>=5.10,<6' websocket-client
fi

exec "$VENV/bin/python" "$TOOL" "$@"
