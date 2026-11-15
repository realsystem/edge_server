# shellcheck shell=bash
# Defaults for every setting. Each one is VAR="${VAR:-default}", so an environment
# variable, the fleet file or the saved deploy.env (all read before this file) wins.

# Tailscale auth key (generate at https://login.tailscale.com/admin/settings/keys)
# Leave empty to skip Tailscale setup or set interactively
TAILSCALE_AUTH_KEY="${TAILSCALE_AUTH_KEY:-}"

# External storage UUID for Frigate video retention
# Find with: lsblk -o NAME,UUID,SIZE,MOUNTPOINT
# Leave empty to be prompted during installation
EXTERNAL_DRIVE_UUID="${EXTERNAL_DRIVE_UUID:-}"

# Storage mount point
STORAGE_MOUNT="${EDGE_STORAGE_DIR:-/mnt/storage}"

# Application directory
APP_DIR="${EDGE_SERVER_DIR:-/opt/edge-server}"
SOURCE_DIR="$APP_DIR/source"  # checkout that --auto updates and deploys from

# Local subnet for firewall rules (CIDR notation)
# Empty (the default) = use the network of the ethernet interface, detected on every
# deploy (resolve_local_subnet). Set it, e.g. LOCAL_SUBNET=10.0.0.0/24, to pin one.
LOCAL_SUBNET="${LOCAL_SUBNET:-}"
LOCAL_SUBNET_REQUEST="$LOCAL_SUBNET"  # what gets saved for --auto: empty = keep detecting

# Timezone: TIMEZONE, else the one this machine is already set to (an install that
# was deployed before keeps its zone), else UTC. A fleet file sets it per site.
system_timezone() {
    local tz=""
    tz=$(timedatectl show -p Timezone --value 2> /dev/null) || tz=""
    [[ -n "$tz" ]] || tz=$(head -n 1 /etc/timezone 2> /dev/null) || tz=""
    if [[ "$tz" =~ ^[A-Za-z][A-Za-z0-9_+/-]*$ && "$tz" != "n/a" ]]; then
        echo "$tz"
    else
        echo UTC
    fi
}
TIMEZONE="${TIMEZONE:-$(system_timezone)}"

# Optional services (off by default). Turning one off on an existing server
# removes its container but keeps its data directory, so it can be re-enabled.
INSTALL_HOMEASSISTANT="${INSTALL_HOMEASSISTANT:-false}"
INSTALL_MQTT="${INSTALL_MQTT:-false}"
# Job processor control plane (github.com/realsystem/processor): a durable job queue that
# workers elsewhere (a GPU machine at home, say) poll over Tailscale. Off by default.
# Its port is published on this node's Tailscale address only (PROCESSOR_BIND overrides).
INSTALL_PROCESSOR="${INSTALL_PROCESSOR:-false}"
PROCESSOR_IMAGE="${PROCESSOR_IMAGE:-ghcr.io/realsystem/processor-control-plane:sha-aa7e243}"
PROCESSOR_PORT="${PROCESSOR_PORT:-8090}"
PROCESSOR_BIND="${PROCESSOR_BIND:-}"
PROCESSOR_ADMIN_EMAIL="${PROCESSOR_ADMIN_EMAIL:-owner@edge.local}"
PROCESSOR_WORKER_AUTH="${PROCESSOR_WORKER_AUTH:-signature}"
# Hardware video decoding (Intel VA-API). Off by default: it fails on some GPUs/
# camera streams (e.g. H.264 profile 100) and software decoding always works.
ENABLE_HWACCEL="${ENABLE_HWACCEL:-false}"
DRI_DEVICE="${DRI_DEVICE:-/dev/dri/renderD128}"  # overridable for tests

# Home Assistant that the bot's /status pings and probes. A fleet file takes it from the
# ha node at this node's site. Without one (classic mode) the check keeps watching the
# address installs made before fleet mode were set up for; HA_HOST= (empty) in the
# environment or in .telegram switches it off, another address replaces it.
LEGACY_HA_HOST="10.0.0.2"
HA_HOST="${HA_HOST-$LEGACY_HA_HOST}"
HA_PORT="${HA_PORT:-8123}"
# Control plane (signed heartbeats between the nodes of a fleet of two or more). Only a
# fleet file turns it on; CONTROL_ACTIVE says it actually got set up on this run.
CONTROL_PLANE="${CONTROL_PLANE:-false}"
CONTROL_PORT="${CONTROL_PORT:-7443}"
CONTROL_ACTIVE=false
CONTROL_PUBLISH=""  # host addresses the control port is published on
NODE_PUBLIC_KEY=""
# Who this machine is in the fleet (labels only: never a path, container or unit name)
EDGE_NODE="${EDGE_NODE:-}"
EDGE_SITE="${EDGE_SITE:-}"
EDGE_OPERATORS="${EDGE_OPERATORS:-}"  # Telegram user ids from the fleet file
EDGE_BOT_ID="${EDGE_BOT_ID:-}"

# Telegram bot (/status, /diag): "auto" installs it when a .telegram file with
# the bot token exists (see README); "false" never installs it.
INSTALL_TELEGRAM_BOT="${INSTALL_TELEGRAM_BOT:-auto}"
INSTALL_TELEGRAM_BOT_REQUEST="$INSTALL_TELEGRAM_BOT"  # prepare_telegram_bot may switch it off
TELEGRAM_IMAGE="edge-telegram-bot:local"
TELEGRAM_CHANGED=false
TELEGRAM_REBOOT_ENABLED=false  # /reboot: needs REBOOT_TOTP_SECRET in .telegram
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

# Frigate image. Pinned: the stack must come up the same way after every boot,
# so a new upstream release never arrives unattended. To upgrade, change this
# and run the deploy script again.
FRIGATE_IMAGE="${FRIGATE_IMAGE:-ghcr.io/blakeblackshear/frigate:0.18.2}"

# Where --auto gets new code, and whether the new commit must have passed CI
# (GitHub check runs) first. VERIFY_TIMEOUT: seconds to wait for the stack to come up.
REDEPLOY_REPO="${REDEPLOY_REPO:-https://github.com/realsystem/edge_server.git}"
# --auto deploys release tags only: v1.0, v1.2, v2.0.1. Not branches, not commit ids,
# not pre-release names. Deliberately a constant, not a setting.
readonly RELEASE_TAG_REGEX='^v[0-9]+\.[0-9]+(\.[0-9]+)?$'
REDEPLOY_REQUIRE_CI="${REDEPLOY_REQUIRE_CI:-true}"
VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-300}"
REDEPLOY_LOCK_FILE="${REDEPLOY_LOCK_FILE:-/run/lock/edge-redeploy.lock}"
ROLLED_BACK=false

# MQTT credentials (only used when INSTALL_MQTT=true)
MQTT_USER="${MQTT_USER:-admin}"
MQTT_PASS="${MQTT_PASS:-}"

# Batch mode - skip interactive prompts (set to "true" for automated runs)
BATCH_MODE="${BATCH_MODE:-false}"

# Frigate config: kept on re-deploy unless this is "true" (old one is backed up)
FRIGATE_CONFIG_OVERWRITE="${FRIGATE_CONFIG_OVERWRITE:-false}"

# What kind of machine this is: "laptop" or "server" (see lib/host.sh). Empty asks on
# a manual run, with the type detected from the hardware as the answer offered; batch
# runs take the detected type. A laptop gets lid/sleep handling and the power loss
# monitor; a server's power settings are left as they are.
HOST_TYPE="${HOST_TYPE:-}"

# Victron Smart Shunt (optional BLE battery monitor)
# Set to "true" to install, or leave empty to be prompted
VICTRON_INSTALL="${VICTRON_INSTALL:-}"
VICTRON_ADDRESS="${VICTRON_ADDRESS:-}"
VICTRON_KEY="${VICTRON_KEY:-}"

# NUT Power Monitor (auto-shutdown on battery)
# Set to "true" to install, or leave empty to be offered it on a laptop with a battery
NUT_POWER_INSTALL="${NUT_POWER_INSTALL:-}"
NUT_SHUTDOWN_DELAY="${NUT_SHUTDOWN_DELAY:-600}"  # Seconds on battery before shutdown
NUT_LOW_BATTERY="${NUT_LOW_BATTERY:-20}"          # Shutdown below this battery %

# Renogy Rover MPPT (optional BLE solar charger monitor)
# Set to "true" to install, or leave empty to be prompted
RENOGY_INSTALL="${RENOGY_INSTALL:-}"
RENOGY_ADDRESS="${RENOGY_ADDRESS:-}"

# Uninstall (--uninstall): "true" removes without asking, "false" skips without
# asking, empty asks. REMOVE_ADDON_CONFIG does the same for the saved settings
# (device key, MQTT password) of the Bluetooth add-ons; they are kept by default.
VICTRON_UNINSTALL="${VICTRON_UNINSTALL:-}"
RENOGY_UNINSTALL="${RENOGY_UNINSTALL:-}"
NUT_POWER_UNINSTALL="${NUT_POWER_UNINSTALL:-}"
REMOVE_ADDON_CONFIG="${REMOVE_ADDON_CONFIG:-}"
