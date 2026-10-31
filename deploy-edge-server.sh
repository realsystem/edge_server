#!/usr/bin/env bash
#===============================================================================
# Edge Server Deployment Script
# Idempotent, production-grade provisioning for Ubuntu Server
#
# Usage: sudo ./deploy-edge-server.sh
#
# Components: Frigate NVR, Tailscale
#   optional: Home Assistant (INSTALL_HOMEASSISTANT=true),
#             Mosquitto MQTT (INSTALL_MQTT=true),
#             Telegram bot (installed when a .telegram file exists)
#===============================================================================

set -euo pipefail
IFS=$'\n\t'

#-------------------------------------------------------------------------------
# CONFIGURATION - Edit these variables before running
#-------------------------------------------------------------------------------

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

# Local subnet for firewall rules (CIDR notation)
LOCAL_SUBNET="${LOCAL_SUBNET:-192.168.0.0/16}"

# Timezone
TIMEZONE="${TIMEZONE:-America/Los_Angeles}"

# Optional services (off by default). Turning one off on an existing server
# removes its container but keeps its data directory, so it can be re-enabled.
INSTALL_HOMEASSISTANT="${INSTALL_HOMEASSISTANT:-false}"
INSTALL_MQTT="${INSTALL_MQTT:-false}"
# Hardware video decoding (Intel VA-API). Off by default: it fails on some GPUs/
# camera streams (e.g. H.264 profile 100) and software decoding always works.
ENABLE_HWACCEL="${ENABLE_HWACCEL:-false}"
DRI_DEVICE="${DRI_DEVICE:-/dev/dri/renderD128}"  # overridable for tests

# Telegram bot (/status, /diag): "auto" installs it when a .telegram file with
# the bot token exists (see README); "false" never installs it.
INSTALL_TELEGRAM_BOT="${INSTALL_TELEGRAM_BOT:-auto}"
TELEGRAM_IMAGE="edge-telegram-bot:local"
TELEGRAM_CHANGED=false

# MQTT credentials (only used when INSTALL_MQTT=true)
MQTT_USER="${MQTT_USER:-admin}"
MQTT_PASS="${MQTT_PASS:-}"

# Batch mode - skip interactive prompts (set to "true" for automated runs)
BATCH_MODE="${BATCH_MODE:-false}"

# Frigate config: kept on re-deploy unless this is "true" (old one is backed up)
FRIGATE_CONFIG_OVERWRITE="${FRIGATE_CONFIG_OVERWRITE:-false}"

# Victron Smart Shunt (optional BLE battery monitor)
# Set to "true" to install, or leave empty to be prompted
VICTRON_INSTALL="${VICTRON_INSTALL:-}"
VICTRON_ADDRESS="${VICTRON_ADDRESS:-}"
VICTRON_KEY="${VICTRON_KEY:-}"

# NUT Power Monitor (auto-shutdown on battery)
# Set to "true" to install on laptop, or leave empty for auto-detect
NUT_POWER_INSTALL="${NUT_POWER_INSTALL:-}"
NUT_SHUTDOWN_DELAY="${NUT_SHUTDOWN_DELAY:-600}"  # Seconds on battery before shutdown
NUT_LOW_BATTERY="${NUT_LOW_BATTERY:-20}"          # Shutdown below this battery %

# Renogy Rover MPPT (optional BLE solar charger monitor)
# Set to "true" to install, or leave empty to be prompted
RENOGY_INSTALL="${RENOGY_INSTALL:-}"
RENOGY_ADDRESS="${RENOGY_ADDRESS:-}"

#-------------------------------------------------------------------------------
# LOGGING & UTILITIES
#-------------------------------------------------------------------------------

readonly LOG_FILE="/var/log/edge-server-deploy.log"
readonly STATUS_FILE="/tmp/edge-server-deploy.status"

log() {
    local level="$1"
    shift
    local message="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    printf '%s [%s] %s\n' "$timestamp" "$level" "$message" | stdbuf -oL tee -a "$LOG_FILE"
    # Update status file for progress monitoring
    printf '%s' "$message" 2>/dev/null > "$STATUS_FILE" || true
}

info()    { log "INFO" "$*"; }
success() { log "OK" "$*"; }
warn()    { log "WARN" "$*"; }
error()   { log "ERROR" "$*"; }
fatal()   { error "$*"; exit 1; }

confirm() {
    local prompt="$1"
    local response
    read -rp "$prompt [y/N]: " response
    [[ "$response" =~ ^[Yy]$ ]]
}

#-------------------------------------------------------------------------------
# PRE-FLIGHT CHECKS
#-------------------------------------------------------------------------------

preflight_checks() {
    info "Running pre-flight checks..."

    # Must run as root
    if [[ $EUID -ne 0 ]]; then
        fatal "This script must be run as root (use sudo)"
    fi

    # Check Ubuntu
    if [[ ! -f /etc/os-release ]] || ! grep -qi ubuntu /etc/os-release; then
        fatal "This script requires Ubuntu Server"
    fi

    # Check internet connectivity
    info "Testing internet connectivity..."
    if ! curl -sf --connect-timeout 10 https://archive.ubuntu.com > /dev/null 2>&1; then
        fatal "No internet connectivity - cannot proceed"
    fi
    success "Internet connectivity confirmed"

    # Check available disk space (need at least 10GB free on /)
    local free_space
    free_space=$(df -BG / | awk 'NR==2 {gsub("G",""); print $4}')
    if [[ "$free_space" -lt 10 ]]; then
        fatal "Insufficient disk space: ${free_space}GB available, need at least 10GB"
    fi
    success "Disk space check passed (${free_space}GB available)"

    # Check memory (need at least 2GB)
    local total_mem
    total_mem=$(awk '/MemTotal/ {print int($2/1024/1024)}' /proc/meminfo)
    if [[ "$total_mem" -lt 2 ]]; then
        warn "Low memory detected: ${total_mem}GB - performance may be impacted"
    else
        success "Memory check passed (${total_mem}GB available)"
    fi

    # Prompt for external drive UUID if not set
    if [[ -z "$EXTERNAL_DRIVE_UUID" ]] && [[ "$BATCH_MODE" != "true" ]]; then
        info "Available block devices:"
        lsblk -o NAME,UUID,SIZE,FSTYPE,MOUNTPOINT
        echo
        read -rp "Enter UUID for external storage drive (or press Enter to skip): " EXTERNAL_DRIVE_UUID
    fi

    # Prompt for MQTT password if not set
    if [[ "$INSTALL_MQTT" == "true" ]] && [[ -z "$MQTT_PASS" ]]; then
        if [[ "$BATCH_MODE" == "true" ]]; then
            MQTT_PASS=$(openssl rand -base64 16)
            info "Generated random MQTT password"
        else
            read -rsp "Enter MQTT password for user '$MQTT_USER': " MQTT_PASS
            echo
            if [[ -z "$MQTT_PASS" ]]; then
                MQTT_PASS=$(openssl rand -base64 16)
                # Not logged: the deploy log is world-readable. Shown once in
                # the summary at the end of the run instead.
                warn "Generated random MQTT password (shown in the summary)"
            fi
        fi
    fi

    # Prompt for Tailscale auth key if not set
    if [[ -z "$TAILSCALE_AUTH_KEY" ]] && [[ "$BATCH_MODE" != "true" ]]; then
        read -rp "Enter Tailscale auth key (or press Enter to skip Tailscale setup): " TAILSCALE_AUTH_KEY
    fi

    success "Pre-flight checks completed"
}

#-------------------------------------------------------------------------------
# SYSTEM HARDENING & POWER MANAGEMENT
#-------------------------------------------------------------------------------

configure_system() {
    info "Configuring system settings..."

    # Set timezone
    timedatectl set-timezone "$TIMEZONE" || true

    # Configure lid switch behavior
    local logind_conf="/etc/systemd/logind.conf"
    info "Configuring lid switch handling..."

    # Backup original if not already backed up
    [[ ! -f "${logind_conf}.orig" ]] && cp "$logind_conf" "${logind_conf}.orig"

    # Set lid switch options idempotently
    for setting in "HandleLidSwitch=ignore" "HandleLidSwitchExternalPower=ignore" "HandleLidSwitchDocked=ignore"; do
        local key="${setting%%=*}"
        if grep -q "^${key}=" "$logind_conf"; then
            sed -i "s/^${key}=.*/${setting}/" "$logind_conf"
        elif grep -q "^#${key}=" "$logind_conf"; then
            sed -i "s/^#${key}=.*/${setting}/" "$logind_conf"
        else
            echo "$setting" >> "$logind_conf"
        fi
    done

    # Disable sleep/suspend/hibernate targets
    info "Disabling sleep, suspend, and hibernate..."
    systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target 2>/dev/null || true

    # Reload logind configuration
    systemctl restart systemd-logind || true

    success "System power management configured"
}

#-------------------------------------------------------------------------------
# PACKAGE INSTALLATION
#-------------------------------------------------------------------------------

install_packages() {
    info "Updating package lists..."
    apt-get update -qq

    info "Installing core packages..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        apt-transport-https \
        ca-certificates \
        curl \
        gnupg \
        lsb-release \
        jq \
        net-tools \
        git \
        unattended-upgrades \
        ufw \
        htop \
        iotop \
        ncdu \
        vim \
        tmux

    # Configure unattended-upgrades
    info "Configuring unattended-upgrades..."
    cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}";
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::SyslogEnable "true";
EOF

    cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

    systemctl enable unattended-upgrades
    systemctl start unattended-upgrades

    success "Core packages installed"
}

install_docker() {
    if command -v docker &> /dev/null; then
        info "Docker already installed, checking version..."
        docker --version
    else
        info "Installing Docker Engine..."

        # Add Docker's official GPG key
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg

        # Add Docker repository
        echo \
            "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
            $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
            tee /etc/apt/sources.list.d/docker.list > /dev/null

        apt-get update -qq
        apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi

    # Add current sudo user to docker group
    local real_user="${SUDO_USER:-$USER}"
    if [[ -n "$real_user" ]] && [[ "$real_user" != "root" ]]; then
        usermod -aG docker "$real_user" 2>/dev/null || true
        info "Added $real_user to docker group"
    fi

    systemctl enable docker
    systemctl start docker

    success "Docker installed and configured"
}

install_tailscale() {
    if command -v tailscale &> /dev/null; then
        info "Tailscale already installed"
    else
        info "Installing Tailscale..."
        curl -fsSL https://tailscale.com/install.sh | sh
    fi

    systemctl enable tailscaled
    systemctl start tailscaled

    # Authenticate if key provided
    if [[ -n "$TAILSCALE_AUTH_KEY" ]]; then
        info "Authenticating Tailscale..."
        tailscale up --authkey="$TAILSCALE_AUTH_KEY" --accept-routes --accept-dns=false || true
    else
        warn "Tailscale installed but not authenticated. Run: sudo tailscale up"
    fi

    success "Tailscale configured"
}

install_victron() {
    # Check if we should install
    if [[ "$VICTRON_INSTALL" != "true" ]]; then
        if [[ "$BATCH_MODE" == "true" ]]; then
            info "Skipping Victron Smart Shunt (VICTRON_INSTALL not set)"
            return 0
        fi

        echo ""
        echo "=== Victron Smart Shunt Monitor (Optional) ==="
        echo "Monitor battery via Bluetooth and publish to MQTT/Home Assistant"
        echo ""
        if ! confirm "Install Victron Smart Shunt monitor?"; then
            info "Skipping Victron Smart Shunt installation"
            return 0
        fi
    fi

    # Get device address if not set
    if [[ -z "$VICTRON_ADDRESS" ]]; then
        if [[ "$BATCH_MODE" == "true" ]]; then
            warn "Skipping Victron: VICTRON_ADDRESS not set in batch mode"
            return 0
        fi
        echo ""
        echo "Get the device address from Victron Connect app:"
        echo "  Device > Settings > Product Info > MAC Address"
        echo ""
        read -rp "Victron MAC address (e.g., AA:BB:CC:DD:EE:FF): " VICTRON_ADDRESS
        if [[ -z "$VICTRON_ADDRESS" ]]; then
            warn "No address provided, skipping Victron installation"
            return 0
        fi
    fi

    # Get encryption key if not set
    if [[ -z "$VICTRON_KEY" ]]; then
        if [[ "$BATCH_MODE" == "true" ]]; then
            warn "Skipping Victron: VICTRON_KEY not set in batch mode"
            return 0
        fi
        echo ""
        echo "Get the encryption key from Victron Connect app:"
        echo "  Device > Settings > Product Info > Encryption data"
        echo ""
        read -rp "Encryption key (32 hex characters): " VICTRON_KEY
        if [[ -z "$VICTRON_KEY" ]]; then
            warn "No key provided, skipping Victron installation"
            return 0
        fi
    fi

    info "Installing Victron Smart Shunt monitor..."
    if [[ "$INSTALL_MQTT" != "true" ]]; then
        warn "MQTT broker not installed - Victron Smart Shunt readings will only be logged"
    fi

    # Find the plugin directory (deployed alongside this script or in plugins/)
    local plugin_dir=""
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [[ -d "${script_dir}/plugins/victron-shunt" ]]; then
        plugin_dir="${script_dir}/plugins/victron-shunt"
    elif [[ -d "${script_dir}/victron-shunt" ]]; then
        plugin_dir="${script_dir}/victron-shunt"
    else
        warn "Victron plugin not found, skipping installation"
        return 0
    fi

    # Run the install script
    export VICTRON_ADDRESS
    export VICTRON_KEY
    export MQTT_USER
    export MQTT_PASS

    if bash "${plugin_dir}/install.sh"; then
        success "Victron Smart Shunt monitor installed"
    else
        warn "Victron installation failed - check logs"
    fi
}

install_nut_power() {
    # Auto-detect laptop (has battery)
    local has_battery=false
    if [ -d /sys/class/power_supply/BAT0 ] || [ -d /sys/class/power_supply/BAT1 ]; then
        has_battery=true
    fi

    # Check if we should install
    if [[ "$NUT_POWER_INSTALL" != "true" ]]; then
        if [[ "$BATCH_MODE" == "true" ]]; then
            if [[ "$has_battery" == "true" ]]; then
                info "Laptop detected but NUT_POWER_INSTALL not set - skipping power monitor"
            fi
            return 0
        fi

        if [[ "$has_battery" != "true" ]]; then
            info "No battery detected - skipping power monitor"
            return 0
        fi

        echo ""
        echo "=== Power Loss Monitor (NUT) ==="
        echo "Laptop battery detected. Install automatic shutdown on power loss?"
        echo "Default: Shutdown after 10 minutes on battery"
        echo ""
        if ! confirm "Install power loss monitor?"; then
            info "Skipping power monitor installation"
            return 0
        fi
    fi

    info "Installing NUT power monitor..."

    # Find the plugin directory
    local plugin_dir=""
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [[ -d "${script_dir}/plugins/nut-power" ]]; then
        plugin_dir="${script_dir}/plugins/nut-power"
    else
        warn "NUT power plugin not found, skipping installation"
        return 0
    fi

    # Set configuration from environment
    export NUT_SHUTDOWN_DELAY="${NUT_SHUTDOWN_DELAY:-600}"
    export NUT_LOW_BATTERY="${NUT_LOW_BATTERY:-20}"
    export BATCH_MODE

    if bash "${plugin_dir}/install.sh"; then
        success "NUT power monitor installed (shutdown after $((NUT_SHUTDOWN_DELAY / 60)) min on battery)"
    else
        warn "NUT installation failed - check logs"
    fi
}

install_renogy() {
    # Check if we should install
    if [[ "$RENOGY_INSTALL" != "true" ]]; then
        if [[ "$BATCH_MODE" == "true" ]]; then
            info "RENOGY_INSTALL not set in batch mode - skipping Renogy monitor"
            return 0
        fi

        echo ""
        echo "=== Renogy Rover MPPT Monitor ==="
        echo "Monitor solar charger data via BLE (requires BT-2 module)"
        echo ""
        if ! confirm "Install Renogy Rover monitor?"; then
            info "Skipping Renogy monitor installation"
            return 0
        fi
    fi

    info "Installing Renogy Rover monitor..."
    if [[ "$INSTALL_MQTT" != "true" ]]; then
        warn "MQTT broker not installed - Renogy Rover readings will only be logged"
    fi

    # Find the plugin directory
    local plugin_dir=""
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [[ -d "${script_dir}/plugins/renogy-rover" ]]; then
        plugin_dir="${script_dir}/plugins/renogy-rover"
    else
        warn "Renogy Rover plugin not found, skipping installation"
        return 0
    fi

    # Pass configuration via environment
    export RENOGY_ADDRESS
    export MQTT_USER
    export MQTT_PASS

    if bash "${plugin_dir}/install.sh"; then
        success "Renogy Rover monitor installed"
    else
        warn "Renogy installation failed - check logs"
    fi
}

#-------------------------------------------------------------------------------
# FIREWALL CONFIGURATION
#-------------------------------------------------------------------------------

configure_firewall() {
    info "Configuring UFW firewall..."

    # Reset to defaults (idempotent)
    ufw --force reset > /dev/null

    # Default policies
    ufw default deny incoming
    ufw default allow outgoing

    # Allow SSH from anywhere (critical for remote access)
    ufw allow ssh comment 'SSH'

    # Allow services from local subnet only
    ufw allow from "$LOCAL_SUBNET" to any port 5000 proto tcp comment 'Frigate'
    if [[ "$INSTALL_MQTT" == "true" ]]; then
        ufw allow from "$LOCAL_SUBNET" to any port 1883 proto tcp comment 'MQTT'
    fi
    if [[ "$INSTALL_HOMEASSISTANT" == "true" ]]; then
        ufw allow from "$LOCAL_SUBNET" to any port 8123 proto tcp comment 'Home Assistant'
    fi

    # Allow Tailscale interface
    ufw allow in on tailscale0 comment 'Tailscale'

    # Enable firewall
    ufw --force enable
    ufw status verbose

    success "Firewall configured"
}

#-------------------------------------------------------------------------------
# STORAGE CONFIGURATION
#-------------------------------------------------------------------------------

configure_storage() {
    info "Configuring external storage..."

    # Create mount point
    mkdir -p "$STORAGE_MOUNT"

    if [[ -z "$EXTERNAL_DRIVE_UUID" ]]; then
        warn "No external drive UUID specified - skipping storage configuration"
        warn "Video recording will use local storage at $STORAGE_MOUNT"
        return 0
    fi

    # Verify the UUID exists
    if ! blkid -U "$EXTERNAL_DRIVE_UUID" &> /dev/null; then
        warn "Drive with UUID $EXTERNAL_DRIVE_UUID not found - skipping fstab entry"
        return 0
    fi

    # Add fstab entry if not present
    local fstab_entry="UUID=${EXTERNAL_DRIVE_UUID} ${STORAGE_MOUNT} auto defaults,nofail,x-systemd.device-timeout=10s,x-systemd.mount-timeout=10s 0 2"

    if grep -q "UUID=${EXTERNAL_DRIVE_UUID}" /etc/fstab; then
        info "fstab entry already exists for UUID ${EXTERNAL_DRIVE_UUID}"
    else
        info "Adding fstab entry..."
        echo "# External storage for Frigate NVR" >> /etc/fstab
        echo "$fstab_entry" >> /etc/fstab
    fi

    # Mount if not already mounted
    if ! mountpoint -q "$STORAGE_MOUNT"; then
        mount "$STORAGE_MOUNT" || warn "Could not mount $STORAGE_MOUNT - will retry on reboot"
    fi

    # Create Frigate directories
    mkdir -p "$STORAGE_MOUNT/frigate/recordings"
    mkdir -p "$STORAGE_MOUNT/frigate/clips"

    success "Storage configured at $STORAGE_MOUNT"
}

#-------------------------------------------------------------------------------
# APPLICATION STACK
#-------------------------------------------------------------------------------

# Turn off MQTT in an existing (kept) Frigate config. Only the exact layout
# this script writes is edited ("mqtt:" followed by "  enabled: true"); the
# file is backed up first. Anything else gets a warning to edit by hand,
# because Frigate keeps retrying a broker that no longer exists.
disable_frigate_mqtt() {
    local cfg="$1" state
    state=$(awk '
        prev == "mqtt:" && /^  enabled:[[:space:]]*true([[:space:]]|$)/  { s = "enabled" }
        prev == "mqtt:" && /^  enabled:[[:space:]]*false([[:space:]]|$)/ { s = "disabled" }
        /^mqtt:/ && s == "" { s = "other" }
        { prev = $0 }
        END { print (s == "" ? "absent" : s) }' "$cfg")
    case "$state" in
        enabled)
            cp -p "$cfg" "${cfg}.bak.$(date +%Y%m%d%H%M%S)"
            awk '{
                if (prev == "mqtt:" && /^  enabled:[[:space:]]*true([[:space:]]|$)/)
                    print "  enabled: false  # no MQTT broker installed (INSTALL_MQTT=false)"
                else
                    print
                prev = $0
            }' "$cfg" > "${cfg}.tmp"
            cat "${cfg}.tmp" > "$cfg"   # keeps owner and permissions
            rm -f "${cfg}.tmp"
            warn "Turned off MQTT in the existing Frigate config (backup saved next to it)"
            ;;
        disabled) ;;
        *)
            warn "MQTT broker is not installed: set 'mqtt: enabled: false' in $cfg"
            ;;
    esac
}

# Older versions of this script wrote "motion: mask: []" for cameras. Current
# Frigate expects masks as a dictionary and refuses to start on the empty
# list, so remove that exact (empty, meaningless) pair from a kept config.
repair_frigate_config() {
    local cfg="$1"
    if grep -qx '      mask: \[\]' "$cfg"; then
        cp -p "$cfg" "${cfg}.bak.$(date +%Y%m%d%H%M%S)"
        sed -i '/^    motion:$/{N;/\n      mask: \[\]$/d}' "$cfg"
        warn "Removed empty 'motion: mask: []' entries Frigate no longer accepts (backup saved)"
    fi
}

# Copy SRC to DST (mode MODE) if it differs; remember that something changed.
install_if_changed() {
    local src="$1" dst="$2" mode="$3"
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        chmod "$mode" "$dst"
        return 0
    fi
    install -m "$mode" "$src" "$dst"
    TELEGRAM_CHANGED=true
}

# Telegram bot: needs a .telegram file with TELEGRAM_BOT_TOKEN (and
# TELEGRAM_ALLOWED_IDS). Sets INSTALL_TELEGRAM_BOT to true only if everything
# is in place, so a missing or wrong token never stops the rest of the deploy.
prepare_telegram_bot() {
    if [[ "$INSTALL_TELEGRAM_BOT" == "false" ]]; then
        info "Telegram bot disabled (INSTALL_TELEGRAM_BOT=false)"
        return 0
    fi
    INSTALL_TELEGRAM_BOT=false  # becomes true below once everything worked

    local script_dir cred="" candidate token ids
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    for candidate in "${TELEGRAM_CREDENTIALS_FILE:-}" "$script_dir/.telegram" "$APP_DIR/.telegram"; do
        if [[ -n "$candidate" && -f "$candidate" ]]; then
            cred="$candidate"
            break
        fi
    done
    if [[ -z "$cred" ]]; then
        info "No .telegram file - skipping the Telegram bot"
        return 0
    fi

    # Read the two values without executing the file
    token=$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$cred" | tail -n 1 | tr -d '\r[:space:]')
    ids=$(sed -n 's/^TELEGRAM_ALLOWED_IDS=//p' "$cred" | tail -n 1 | tr -d '\r')
    local token_re='^[0-9]{5,}:[A-Za-z0-9_-]{20,}$' ids_re='^[0-9]+([, ]+[0-9]+)*$'
    if [[ ! "$token" =~ $token_re ]]; then
        warn "$cred: TELEGRAM_BOT_TOKEN is missing or not a bot token (123456:ABC...) - skipping the Telegram bot"
        return 0
    fi
    if [[ -z "${ids//[[:space:]]/}" ]]; then
        warn "TELEGRAM_ALLOWED_IDS is empty: nobody can use the bot yet. Send /id to it, then add your id to .telegram and redeploy"
    elif [[ ! "$ids" =~ $ids_re ]]; then
        warn "$cred: TELEGRAM_ALLOWED_IDS must be numeric user ids separated by commas - skipping the Telegram bot"
        return 0
    fi
    if [[ ! -f "$script_dir/telegram-bot/bot.py" || ! -f "$script_dir/telegram-bot/Dockerfile" \
        || ! -f "$script_dir/collect-diagnostics.sh" ]]; then
        warn "telegram-bot/ or collect-diagnostics.sh missing next to the script - skipping the Telegram bot"
        return 0
    fi

    info "Installing the Telegram bot..."
    TELEGRAM_CHANGED=false
    mkdir -p "$APP_DIR/telegram-bot"
    chmod 755 "$APP_DIR" "$APP_DIR/telegram-bot"
    install_if_changed "$script_dir/telegram-bot/bot.py" "$APP_DIR/telegram-bot/bot.py" 644
    install_if_changed "$script_dir/telegram-bot/Dockerfile" "$APP_DIR/telegram-bot/Dockerfile" 644
    install_if_changed "$script_dir/collect-diagnostics.sh" "$APP_DIR/telegram-bot/collect-diagnostics.sh" 755
    # The token is a secret: root-only, and never written into the compose file
    if [[ "$cred" == "$APP_DIR/.telegram" ]]; then
        chmod 600 "$APP_DIR/.telegram"
    else
        install_if_changed "$cred" "$APP_DIR/.telegram" 600
    fi

    # Only OS tools go into the image (the bot code is mounted), so this is
    # quick after the first build.
    if docker build -q -t "$TELEGRAM_IMAGE" "$APP_DIR/telegram-bot" > /dev/null; then
        INSTALL_TELEGRAM_BOT=true
    elif docker image inspect "$TELEGRAM_IMAGE" &> /dev/null; then
        warn "Could not rebuild the Telegram bot image - keeping the existing one"
        INSTALL_TELEGRAM_BOT=true
    else
        warn "Could not build the Telegram bot image (no network?) - skipping the Telegram bot"
    fi
}

create_app_stack() {
    info "Creating application stack at $APP_DIR..."

    mkdir -p "$APP_DIR/frigate"
    [[ "$INSTALL_MQTT" == "true" ]] && mkdir -p "$APP_DIR"/mosquitto/{config,data,log}
    [[ "$INSTALL_HOMEASSISTANT" == "true" ]] && mkdir -p "$APP_DIR/ha-config"
    # Only the directories themselves: a recursive chmod on re-deploy would make
    # Home Assistant's .storage (auth tokens) and other secrets world-readable.
    chmod 755 "$APP_DIR" "$APP_DIR/frigate"
    [[ -d "$APP_DIR/mosquitto" ]] && chmod 755 "$APP_DIR/mosquitto"
    [[ -d "$APP_DIR/ha-config" ]] && chmod 700 "$APP_DIR/ha-config"

    # Remember the Frigate config as it was, so start_services can restart
    # Frigate if this run changes it (MQTT setting, discovered cameras).
    FRIGATE_CONFIG_HASH_BEFORE=""
    if [[ -f "$APP_DIR/frigate/config.yml" ]]; then
        FRIGATE_CONFIG_HASH_BEFORE=$(sha256sum "$APP_DIR/frigate/config.yml" | cut -d' ' -f1)
    fi

    #---------------------------------------------------------------------------
    # Docker Compose
    #---------------------------------------------------------------------------
    {
        echo "services:"
        if [[ "$INSTALL_MQTT" == "true" ]]; then
            cat << 'EOF'
  #-----------------------------------------------------------------------------
  # Eclipse Mosquitto MQTT Broker
  #-----------------------------------------------------------------------------
  mosquitto:
    image: eclipse-mosquitto:2
    container_name: mosquitto
    restart: unless-stopped
    ports:
      - "1883:1883"
    volumes:
      - ./mosquitto/config:/mosquitto/config:ro
      - ./mosquitto/data:/mosquitto/data
      - ./mosquitto/log:/mosquitto/log
    user: "1883:1883"

EOF
        fi
        if [[ "$INSTALL_HOMEASSISTANT" == "true" ]]; then
            cat << 'EOF'
  #-----------------------------------------------------------------------------
  # Home Assistant Core
  #-----------------------------------------------------------------------------
  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:stable
    container_name: homeassistant
    restart: unless-stopped
    ports:
      - "8123:8123"
    privileged: true
    environment:
      - TZ=${TIMEZONE:-America/Los_Angeles}
    volumes:
      - ./ha-config:/config
      - /etc/localtime:/etc/localtime:ro
EOF
            if [[ "$INSTALL_MQTT" == "true" ]]; then
                printf '    depends_on:\n      - mosquitto\n'
            fi
            echo
        fi
        cat << 'EOF'
  #-----------------------------------------------------------------------------
  # Frigate NVR
  #-----------------------------------------------------------------------------
  frigate:
    image: ghcr.io/blakeblackshear/frigate:stable
    container_name: frigate
    restart: unless-stopped
    privileged: true
    shm_size: "256mb"
    ports:
      - "5000:5000"   # Web UI
      - "8554:8554"   # RTSP restream
      - "8555:8555/tcp" # WebRTC over TCP
      - "8555:8555/udp" # WebRTC over UDP
    environment:
      - TZ=${TIMEZONE:-America/Los_Angeles}
      - FRIGATE_RTSP_PASSWORD=${FRIGATE_RTSP_PASSWORD:-}
    volumes:
      # Whole directory, writable: Frigate saves settings from its UI to
      # config.yml and keeps its database (events, recording index) here.
      - ./frigate:/config
      - __STORAGE_MOUNT__/frigate:/media/frigate
      - type: tmpfs
        target: /tmp/cache
        tmpfs:
          size: 1000000000
    devices:
      - /dev/dri/renderD128:/dev/dri/renderD128  # Intel QuickSync (__HWACCEL__)
EOF
        if [[ "$INSTALL_MQTT" == "true" ]]; then
            printf '    depends_on:\n      - mosquitto\n'
        fi
        if [[ "$INSTALL_TELEGRAM_BOT" == "true" ]]; then
            cat << 'EOF'

  #-----------------------------------------------------------------------------
  # Telegram bot (/status, /diag). Outbound HTTPS only; no ports. Read-only
  # view of the host (pid: host, /proc, /sys) for the diagnostics report.
  #-----------------------------------------------------------------------------
  telegram-bot:
    image: edge-telegram-bot:local
    pull_policy: never
    container_name: telegram-bot
    restart: unless-stopped
    init: true
    pid: host
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    env_file: ./.telegram
    environment:
      - TZ=${TIMEZONE:-America/Los_Angeles}
      - FRIGATE_URL=http://frigate:5000
      - EDGE_SERVER_DIR=/opt/edge-server
      - STORAGE_PATH=/media/frigate
    volumes:
      - ./telegram-bot:/app:ro
      - .:/opt/edge-server:ro
      - __STORAGE_MOUNT__/frigate:/media/frigate:ro
    tmpfs:
      - /tmp
    mem_limit: 256m
    pids_limit: 128
EOF
        fi
        cat << 'EOF'

networks:
  default:
    name: edge-server
    driver: bridge
EOF
    } > "$APP_DIR/docker-compose.yml"

    # Substitute storage mount path
    sed -i "s|__STORAGE_MOUNT__|${STORAGE_MOUNT}|g" "$APP_DIR/docker-compose.yml"

    # Without an Intel GPU the device mapping makes `docker compose up` fail
    # outright, so drop it (and VA-API below) on machines that lack it.
    local ffmpeg_block=$'ffmpeg:\n  hwaccel_args: preset-vaapi'
    if [[ "$ENABLE_HWACCEL" == "true" && -e "$DRI_DEVICE" ]]; then
        sed -i "s| (__HWACCEL__)||" "$APP_DIR/docker-compose.yml"
    else
        if [[ "$ENABLE_HWACCEL" == "true" ]]; then
            warn "ENABLE_HWACCEL=true but no /dev/dri/renderD128 - using software decoding"
        else
            info "Hardware acceleration off (software decoding); set ENABLE_HWACCEL=true to enable"
        fi
        sed -i '/__HWACCEL__/d; /^    devices:$/d' "$APP_DIR/docker-compose.yml"
        # No `ffmpeg:` key at all: a key with only a comment under it is null,
        # which Frigate's config loader crashes on.
        ffmpeg_block="# hardware acceleration (preset-vaapi) off: set ENABLE_HWACCEL=true to enable"
    fi

    #---------------------------------------------------------------------------
    # Mosquitto Configuration
    #---------------------------------------------------------------------------
    if [[ "$INSTALL_MQTT" == "true" ]]; then
    cat > "$APP_DIR/mosquitto/config/mosquitto.conf" << 'EOF'
# Mosquitto MQTT Broker Configuration
persistence true
persistence_location /mosquitto/data/

log_dest file /mosquitto/log/mosquitto.log
log_type error
log_type warning
log_type notice
log_type information

listener 1883
protocol mqtt

# Authentication
allow_anonymous false
password_file /mosquitto/config/passwd
EOF

    # Create password file
    info "Creating Mosquitto password file..."
    docker run --rm -v "$APP_DIR/mosquitto/config:/mosquitto/config" \
        eclipse-mosquitto:2 mosquitto_passwd -b -c /mosquitto/config/passwd "$MQTT_USER" "$MQTT_PASS"
    chown 1883:1883 "$APP_DIR/mosquitto/config/passwd"
    chmod 600 "$APP_DIR/mosquitto/config/passwd"
    chown -R 1883:1883 "$APP_DIR/mosquitto"
    fi

    #---------------------------------------------------------------------------
    # Frigate Configuration
    #---------------------------------------------------------------------------
    # An existing config holds your cameras, zones and masks, so a re-deploy
    # (update) keeps it and writes the current template to config.yml.new for
    # reference. Set FRIGATE_CONFIG_OVERWRITE=true to regenerate it instead;
    # the old file is then backed up to config.yml.bak.<timestamp>.
    local frigate_cfg="$APP_DIR/frigate/config.yml"
    local frigate_out="$frigate_cfg"
    if [[ -d "$frigate_cfg" ]]; then
        # Docker creates an empty directory when the bind-mount source is
        # missing; replace it with the real file.
        rmdir "$frigate_cfg" 2>/dev/null || true
    fi
    if [[ -f "$frigate_cfg" ]]; then
        if [[ "$FRIGATE_CONFIG_OVERWRITE" == "true" ]]; then
            cp -p "$frigate_cfg" "${frigate_cfg}.bak.$(date +%Y%m%d%H%M%S)"
            warn "Regenerating Frigate config (previous one backed up)"
        else
            frigate_out="${frigate_cfg}.new"
        fi
    fi
    local mqtt_block
    if [[ "$INSTALL_MQTT" == "true" ]]; then
        mqtt_block="mqtt:
  enabled: true
  host: mosquitto
  port: 1883
  user: ${MQTT_USER}
  password: ${MQTT_PASS}
  topic_prefix: frigate
  stats_interval: 60"
    else
        mqtt_block="mqtt:
  enabled: false  # no MQTT broker installed (INSTALL_MQTT=false)"
    fi

    cat > "$frigate_out" << EOF
# Frigate NVR Configuration
# Documentation: https://docs.frigate.video/

${mqtt_block}

detectors:
  cpu1:
    type: cpu
    num_threads: 2

${ffmpeg_block}

record:
  enabled: true

snapshots:
  enabled: true

cameras:
  # Placeholder camera - replace with your actual camera
  placeholder_camera:
    enabled: false
    ffmpeg:
      inputs:
        - path: rtsp://user:password@camera-ip:554/stream
          roles:
            - detect
            - record
    detect:
      width: 1280
      height: 720
      fps: 5
    objects:
      track:
        - person
        - car
        - dog
        - cat

  # Example: Add your cameras here
  # barn_cam:
  #   enabled: true
  #   ffmpeg:
  #     inputs:
  #       - path: rtsp://admin:password@192.168.1.100:554/cam/realmonitor
  #         roles:
  #           - detect
  #           - record
  #   detect:
  #     width: 1920
  #     height: 1080
  #     fps: 5
  #   objects:
  #     track:
  #       - person
  #       - car
  #       - dog
  #       - cat
  #       - horse
  #       - cow
EOF

    chmod 600 "$frigate_out"  # contains credentials
    if [[ "$frigate_out" != "$frigate_cfg" ]]; then
        chmod 600 "$frigate_cfg"
        info "Kept existing Frigate config: $frigate_cfg"
        info "Latest template written to ${frigate_out} (FRIGATE_CONFIG_OVERWRITE=true to replace)"
        if [[ "$INSTALL_MQTT" != "true" ]]; then
            disable_frigate_mqtt "$frigate_cfg"
        fi
        repair_frigate_config "$frigate_cfg"
    else
        info "Created Frigate config: $frigate_cfg"
    fi

    #---------------------------------------------------------------------------
    # Home Assistant Initial Configuration
    #---------------------------------------------------------------------------
    if [[ "$INSTALL_HOMEASSISTANT" == "true" ]] && [[ ! -f "$APP_DIR/ha-config/configuration.yaml" ]]; then
        cat > "$APP_DIR/ha-config/configuration.yaml" << 'EOF'
# Home Assistant Configuration
# Documentation: https://www.home-assistant.io/docs/configuration/

homeassistant:
  name: Edge Server
  unit_system: imperial
  time_zone: America/Los_Angeles

default_config:

http:
  server_port: 8123
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
    - ::1
    - 100.64.0.0/10  # Tailscale

logger:
  default: info

recorder:
  purge_keep_days: 14
  commit_interval: 30

# MQTT and NUT can no longer be configured in YAML (Home Assistant rejects
# `mqtt: broker:` and `sensor: - platform: nut`). Add them in the UI:
#   Settings -> Devices & Services -> Add Integration -> MQTT
#     broker: mosquitto, port: 1883, MQTT user/password from the deploy summary
#   Settings -> Devices & Services -> Add Integration -> Network UPS Tools
#     host: 172.17.0.1 (Docker host), port: 3493
EOF
        info "Created initial Home Assistant configuration"
    fi

    success "Application stack created at $APP_DIR"
}

#-------------------------------------------------------------------------------
# CAMERA DISCOVERY
#-------------------------------------------------------------------------------

# Find ONVIF cameras and add new ones to the Frigate config, using the logins
# in a .cam file ("IP user:pass" per camera, see README). Skipped when there is no .cam file.
discover_cameras() {
    local script_dir cred_file="" candidate
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    for candidate in "${CAM_CREDENTIALS_FILE:-}" "$script_dir/.cam" "$APP_DIR/.cam"; do
        if [[ -n "$candidate" && -f "$candidate" ]]; then
            cred_file="$candidate"
            break
        fi
    done
    if [[ -z "$cred_file" ]]; then
        info "No .cam file with camera logins - skipping camera discovery"
        return 0
    fi

    # Keep a root-only copy next to the stack for later discover-cameras.sh runs
    if [[ "$cred_file" != "$APP_DIR/.cam" ]]; then
        install -m 600 "$cred_file" "$APP_DIR/.cam"
    fi
    chmod 600 "$APP_DIR/.cam"

    if [[ ! -f "$script_dir/tools/camera_discovery.py" ]] || ! command -v python3 &>/dev/null; then
        warn "Camera discovery tool or python3 missing - skipping camera discovery"
        return 0
    fi

    info "Discovering cameras on the network..."
    if ! python3 "$script_dir/tools/camera_discovery.py" \
        --credentials "$APP_DIR/.cam" --config "$APP_DIR/frigate/config.yml" --apply; then
        warn "Camera discovery failed - add cameras to $APP_DIR/frigate/config.yml by hand"
    fi
}

#-------------------------------------------------------------------------------
# SYSTEMD SERVICE
#-------------------------------------------------------------------------------

create_systemd_service() {
    info "Creating systemd service for auto-start..."

    cat > /etc/systemd/system/edge-server.service << EOF
[Unit]
Description=Edge Server Docker Compose Stack
Documentation=https://docs.frigate.video
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${APP_DIR}
ExecStartPre=/usr/bin/docker compose pull --quiet
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose down
ExecReload=/usr/bin/docker compose up -d --remove-orphans
TimeoutStartSec=300
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

    # Create timer for periodic health checks
    cat > /etc/systemd/system/edge-server-health.service << EOF
[Unit]
Description=Edge Server Health Check
After=edge-server.service

[Service]
Type=oneshot
ExecStart=${APP_DIR}/health-check.sh
EOF

    cat > /etc/systemd/system/edge-server-health.timer << 'EOF'
[Unit]
Description=Run Edge Server Health Check every 5 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF

    # Create health check script
    cat > "$APP_DIR/health-check.sh" << 'EOF'
#!/usr/bin/env bash
# Edge Server Health Check

set -euo pipefail

COMPOSE_DIR="__APP_DIR__"
STORAGE_MOUNT="__STORAGE_MOUNT__"
CONTAINERS=(__CONTAINERS__)
LOG_FILE="/var/log/edge-server-health.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

cd "$COMPOSE_DIR"

# Check each container that is part of this deployment
for container in "${CONTAINERS[@]}"; do
    status=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
    if [[ "$status" == "restarting" ]]; then
        log "Container $container is in a restart loop"
    elif [[ "$status" != "running" ]]; then
        log "Container $container is $status - restarting stack"
        docker compose up -d --remove-orphans
        exit 0
    fi
done

# Check disk space on storage
if [[ -d "$STORAGE_MOUNT" ]]; then
    used_percent=$(df "$STORAGE_MOUNT" 2>/dev/null | awk 'NR==2 {print $5}' | tr -d '%')
    if [[ -n "$used_percent" ]] && [[ "$used_percent" -gt 90 ]]; then
        log "WARNING: Storage at ${used_percent}% capacity"
    fi
fi

log "Health check passed - all containers running"
EOF

    local containers="frigate"
    [[ "$INSTALL_MQTT" == "true" ]] && containers="$containers mosquitto"
    [[ "$INSTALL_HOMEASSISTANT" == "true" ]] && containers="$containers homeassistant"
    [[ "$INSTALL_TELEGRAM_BOT" == "true" ]] && containers="$containers telegram-bot"
    sed -i -e "s|__APP_DIR__|${APP_DIR}|" -e "s|__STORAGE_MOUNT__|${STORAGE_MOUNT}|" \
        -e "s|__CONTAINERS__|${containers}|" "$APP_DIR/health-check.sh"
    chmod +x "$APP_DIR/health-check.sh"

    # Reload and enable services
    systemctl daemon-reload
    systemctl enable edge-server.service
    systemctl enable edge-server-health.timer

    success "Systemd services created and enabled"
}

#-------------------------------------------------------------------------------
# START SERVICES
#-------------------------------------------------------------------------------

start_services() {
    info "Starting application stack..."

    cd "$APP_DIR"

    # Pull images (the Telegram bot image is built locally, there is nothing to pull)
    local -a pullable
    mapfile -t pullable < <(docker compose config --services | grep -vx telegram-bot || true)
    docker compose pull "${pullable[@]}"

    # Start stack. --remove-orphans stops and removes services that were
    # turned off (e.g. Home Assistant/Mosquitto); their data stays on disk.
    docker compose up -d --remove-orphans

    # Frigate only reads its config at start-up
    if [[ -n "${FRIGATE_CONFIG_HASH_BEFORE:-}" ]] && [[ -f "$APP_DIR/frigate/config.yml" ]]; then
        local hash_now
        hash_now=$(sha256sum "$APP_DIR/frigate/config.yml" | cut -d' ' -f1)
        if [[ "$hash_now" != "$FRIGATE_CONFIG_HASH_BEFORE" ]]; then
            info "Frigate config changed - restarting Frigate"
            docker compose restart frigate
        fi
    fi

    # The bot code is mounted and its token comes from .telegram: recreate it
    # when either changed
    if [[ "$INSTALL_TELEGRAM_BOT" == "true" && "$TELEGRAM_CHANGED" == "true" ]]; then
        info "Telegram bot files changed - recreating the bot"
        docker compose up -d --force-recreate --no-deps telegram-bot
    fi

    # Wait for containers to start
    sleep 10

    # Show status
    docker compose ps

    # Start health check timer
    systemctl start edge-server-health.timer

    success "Services started"
}

#-------------------------------------------------------------------------------
# POST-INSTALL SUMMARY
#-------------------------------------------------------------------------------

show_summary() {
    local ip_addr
    ip_addr=$(hostname -I | awk '{print $1}')

    echo
    echo "=============================================================================="
    echo "Edge Server Deployment Complete"
    echo "=============================================================================="
    echo
    echo "Access URLs (replace with Tailscale IP for remote access):"
    echo "  - Frigate NVR:    http://${ip_addr}:5000"
    [[ "$INSTALL_HOMEASSISTANT" == "true" ]] && echo "  - Home Assistant: http://${ip_addr}:8123"
    [[ "$INSTALL_TELEGRAM_BOT" == "true" ]] && echo "  - Telegram bot:   send /status or /diag to your bot"
    if [[ "$INSTALL_MQTT" == "true" ]]; then
        echo "  - MQTT Broker:    mqtt://${ip_addr}:1883"
        echo
        echo "Credentials:"
        echo "  - MQTT User:     ${MQTT_USER}"
        echo "  - MQTT Password: ${MQTT_PASS}"
    fi
    echo
    echo "Configuration Files:"
    echo "  - Docker Compose:  ${APP_DIR}/docker-compose.yml"
    echo "  - Frigate Config:  ${APP_DIR}/frigate/config.yml"
    [[ "$INSTALL_HOMEASSISTANT" == "true" ]] && echo "  - HA Config:       ${APP_DIR}/ha-config/configuration.yaml"
    [[ "$INSTALL_MQTT" == "true" ]] && echo "  - Mosquitto:       ${APP_DIR}/mosquitto/config/mosquitto.conf"
    echo
    echo "Storage:"
    echo "  - Video Storage:   ${STORAGE_MOUNT}/frigate"
    echo
    echo "Management Commands:"
    echo "  - View logs:       cd ${APP_DIR} && docker compose logs -f"
    echo "  - Restart stack:   sudo systemctl restart edge-server"
    echo "  - Stack status:    cd ${APP_DIR} && docker compose ps"
    echo "  - Update images:   cd ${APP_DIR} && docker compose pull && docker compose up -d"
    if systemctl is-enabled victron-shunt &>/dev/null; then
        echo ""
        echo "Victron Smart Shunt:"
        echo "  - View logs:       journalctl -u victron-shunt -f"
        echo "  - MQTT topics:     victron/smartshunt/voltage, current, soc, power"
        echo "  - Config:          /etc/victron-shunt/config.yaml"
    fi
    echo
    echo "Next Steps:"
    echo "  - Cameras: put camera logins in ${APP_DIR}/.cam (\"IP user:pass\" per line) and run"
    echo "    sudo ./discover-cameras.sh, or edit ${APP_DIR}/frigate/config.yml"
    if [[ "$INSTALL_HOMEASSISTANT" == "true" ]]; then
        echo "  - Complete Home Assistant onboarding at http://${ip_addr}:8123"
    fi
    if [[ -z "$TAILSCALE_AUTH_KEY" ]]; then
        echo "  - Authenticate Tailscale: sudo tailscale up"
    else
        echo "  - Verify Tailscale: tailscale status"
    fi
    echo
    echo "Log file: ${LOG_FILE}"
    echo "=============================================================================="
}

#-------------------------------------------------------------------------------
# MAIN
#-------------------------------------------------------------------------------

main() {
    echo
    echo "=============================================================================="
    echo "Edge Server Deployment Script"
    echo "=============================================================================="
    echo

    # Root check before anything touches /var/log
    if [[ $EUID -ne 0 ]]; then
        echo "This script must be run as root (use sudo)" >&2
        exit 1
    fi

    # Initialize log
    mkdir -p "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE"
    chmod 644 "$LOG_FILE"

    info "Starting deployment at $(date)"
    info "Log file: $LOG_FILE"

    preflight_checks
    configure_system
    install_packages
    install_docker
    install_tailscale
    configure_firewall
    configure_storage
    prepare_telegram_bot
    create_app_stack
    discover_cameras
    create_systemd_service
    start_services
    install_victron
    install_nut_power
    install_renogy
    show_summary

    info "Deployment completed successfully at $(date)"
}

# Only run when executed, so tests can `source` this file and call functions.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
