#!/usr/bin/env bash
# Renders the node's real docker-compose.yml and config with the PRODUCTION deploy script
# (prepare_telegram_bot, create_app_stack), in a throwaway container, so the lab runs the
# compose file production would get. Nothing is installed, no service is touched: docker,
# systemctl, tailscale, iptables and ufw are no-op stubs in here.
#
# Inputs (environment): APP (state dir, same path on host and here), REPO (this checkout),
# CRED (a .telegram file). Run as root in the container; works anywhere with bash and openssl.
set -euo pipefail

: "${APP:?}" "${REPO:?}" "${CRED:?}"
SHIMS="$(mktemp -d)"
for c in docker systemctl tailscale iptables ufw; do
  printf '#!/bin/sh\nexit 0\n' > "$SHIMS/$c"
  chmod +x "$SHIMS/$c"
done
export PATH="$SHIMS:$PATH"
export EDGE_SERVER_DIR="$APP" SYSTEMD_DIR="$SHIMS/systemd" EDGE_STORAGE_DIR="$APP/storage" \
  TIMEZONE=UTC HA_HOST= BATCH_MODE=true INSTALL_PROCESSOR=false INSTALL_MQTT=false \
  INSTALL_HOMEASSISTANT=false TELEGRAM_CREDENTIALS_FILE="$CRED"
mkdir -p "$APP" "$EDGE_STORAGE_DIR/frigate"

# shellcheck source=/dev/null
source "$REPO/deploy-edge-server.sh"
log() { printf '%s\n' "$*"; }
info() { printf 'render: %s\n' "$*"; }
warn() { printf 'render: WARN %s\n' "$*"; }
success() { printf 'render: %s\n' "$*"; }
error() { printf 'render: ERROR %s\n' "$*"; }

prepare_telegram_bot
[[ "$INSTALL_TELEGRAM_BOT" == "true" ]] || { echo "render: the bot was not set up (check the token and ids)" >&2; exit 1; }
create_app_stack
# made by root in here; the host user, other containers and the bot must be able to use it
chmod 755 "$APP"
chmod -R a+rX "$APP/telegram-bot" "$APP/frigate" "$APP/bot-status" 2> /dev/null || true
chmod 777 "$APP/bot-requests" "$APP/bot-state" 2> /dev/null || true
# compose reads these as the host user; the token is the lab bot's
chmod 644 "$APP/.telegram" "$APP/bot-defaults.env" 2> /dev/null || true
echo "render: done: $APP/docker-compose.yml"
