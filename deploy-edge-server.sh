#!/usr/bin/env bash
#===============================================================================
# Edge Server Deployment Script
# Idempotent, production-grade provisioning for Ubuntu Server
#
# Usage: sudo ./deploy-edge-server.sh
#
# Components: Frigate NVR, Home Assistant, Mosquitto MQTT, Tailscale
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

# MQTT credentials
MQTT_USER="${MQTT_USER:-admin}"
MQTT_PASS="${MQTT_PASS:-}"

# Batch mode - skip interactive prompts (set to "true" for automated runs)
BATCH_MODE="${BATCH_MODE:-false}"

# Victron Smart Shunt (optional BLE battery monitor)
# Set to "true" to install, or leave empty to be prompted
VICTRON_INSTALL="${VICTRON_INSTALL:-}"
VICTRON_ADDRESS="${VICTRON_ADDRESS:-}"
VICTRON_KEY="${VICTRON_KEY:-}"

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
    printf '%s' "$message" > "$STATUS_FILE"
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
    if [[ -z "$MQTT_PASS" ]]; then
        if [[ "$BATCH_MODE" == "true" ]]; then
            MQTT_PASS=$(openssl rand -base64 16)
            info "Generated random MQTT password"
        else
            read -rsp "Enter MQTT password for user '$MQTT_USER': " MQTT_PASS
            echo
            if [[ -z "$MQTT_PASS" ]]; then
                MQTT_PASS=$(openssl rand -base64 16)
                warn "Generated random MQTT password: $MQTT_PASS"
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
    ufw allow from "$LOCAL_SUBNET" to any port 1883 proto tcp comment 'MQTT'
    ufw allow from "$LOCAL_SUBNET" to any port 8123 proto tcp comment 'Home Assistant'
    ufw allow from "$LOCAL_SUBNET" to any port 5000 proto tcp comment 'Frigate'

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

create_app_stack() {
    info "Creating application stack at $APP_DIR..."

    mkdir -p "$APP_DIR"/{mosquitto/{config,data,log},ha-config,frigate}
    chmod -R 755 "$APP_DIR"

    #---------------------------------------------------------------------------
    # Docker Compose
    #---------------------------------------------------------------------------
    cat > "$APP_DIR/docker-compose.yml" << 'EOF'
services:
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
    depends_on:
      - mosquitto

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
      - ./frigate/config.yml:/config/config.yml:ro
      - __STORAGE_MOUNT__/frigate:/media/frigate
      - type: tmpfs
        target: /tmp/cache
        tmpfs:
          size: 1000000000
    devices:
      - /dev/dri/renderD128:/dev/dri/renderD128  # Intel QuickSync
    depends_on:
      - mosquitto

networks:
  default:
    name: edge-server
    driver: bridge
EOF

    # Substitute storage mount path
    sed -i "s|__STORAGE_MOUNT__|${STORAGE_MOUNT}|g" "$APP_DIR/docker-compose.yml"

    #---------------------------------------------------------------------------
    # Mosquitto Configuration
    #---------------------------------------------------------------------------
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

    #---------------------------------------------------------------------------
    # Frigate Configuration
    #---------------------------------------------------------------------------
    cat > "$APP_DIR/frigate/config.yml" << EOF
# Frigate NVR Configuration
# Documentation: https://docs.frigate.video/

mqtt:
  enabled: true
  host: mosquitto
  port: 1883
  user: ${MQTT_USER}
  password: ${MQTT_PASS}
  topic_prefix: frigate
  stats_interval: 60

detectors:
  cpu1:
    type: cpu
    num_threads: 2

ffmpeg:
  hwaccel_args: preset-vaapi

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
    motion:
      mask: []

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

    #---------------------------------------------------------------------------
    # Home Assistant Initial Configuration
    #---------------------------------------------------------------------------
    if [[ ! -f "$APP_DIR/ha-config/configuration.yaml" ]]; then
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

mqtt:
  broker: 127.0.0.1
  port: 1883
  discovery: true
  discovery_prefix: homeassistant
EOF
        info "Created initial Home Assistant configuration"
    fi

    # Set correct ownership for mosquitto
    chown -R 1883:1883 "$APP_DIR/mosquitto"

    success "Application stack created at $APP_DIR"
}

#-------------------------------------------------------------------------------
# SYSTEMD SERVICE
#-------------------------------------------------------------------------------

create_systemd_service() {
    info "Creating systemd service for auto-start..."

    cat > /etc/systemd/system/edge-server.service << EOF
[Unit]
Description=Edge Server Docker Compose Stack
Documentation=https://docs.frigate.video https://home-assistant.io
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
    cat > /etc/systemd/system/edge-server-health.service << 'EOF'
[Unit]
Description=Edge Server Health Check
After=edge-server.service

[Service]
Type=oneshot
ExecStart=/opt/edge-server/health-check.sh
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

COMPOSE_DIR="/opt/edge-server"
LOG_FILE="/var/log/edge-server-health.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

cd "$COMPOSE_DIR"

# Check each container
for container in mosquitto homeassistant frigate; do
    status=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
    if [[ "$status" != "running" ]]; then
        log "Container $container is $status - restarting stack"
        docker compose up -d --remove-orphans
        exit 0
    fi
done

# Check if containers are healthy (not restarting)
restart_count=$(docker ps --filter "name=mosquitto" --filter "name=homeassistant" --filter "name=frigate" --format "{{.Status}}" | grep -c "Restarting" || true)
if [[ "$restart_count" -gt 0 ]]; then
    log "Detected $restart_count containers in restart loop"
fi

# Check disk space on storage
if [[ -d "/mnt/storage" ]]; then
    used_percent=$(df /mnt/storage 2>/dev/null | awk 'NR==2 {print $5}' | tr -d '%')
    if [[ -n "$used_percent" ]] && [[ "$used_percent" -gt 90 ]]; then
        log "WARNING: Storage at ${used_percent}% capacity"
    fi
fi

log "Health check passed - all containers running"
EOF

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

    # Pull images
    docker compose pull

    # Start stack
    docker compose up -d

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
    echo "  - Home Assistant: http://${ip_addr}:8123"
    echo "  - Frigate NVR:    http://${ip_addr}:5000"
    echo "  - MQTT Broker:    mqtt://${ip_addr}:1883"
    echo
    echo "Credentials:"
    echo "  - MQTT User:     ${MQTT_USER}"
    echo "  - MQTT Password: ${MQTT_PASS}"
    echo
    echo "Configuration Files:"
    echo "  - Docker Compose:  ${APP_DIR}/docker-compose.yml"
    echo "  - Frigate Config:  ${APP_DIR}/frigate/config.yml"
    echo "  - HA Config:       ${APP_DIR}/ha-config/configuration.yaml"
    echo "  - Mosquitto:       ${APP_DIR}/mosquitto/config/mosquitto.conf"
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
    echo "  1. Configure your cameras in ${APP_DIR}/frigate/config.yml"
    echo "  2. Complete Home Assistant onboarding at http://${ip_addr}:8123"
    echo "  3. Add MQTT integration in Home Assistant"
    if [[ -z "$TAILSCALE_AUTH_KEY" ]]; then
        echo "  4. Authenticate Tailscale: sudo tailscale up"
    else
        echo "  4. Verify Tailscale: tailscale status"
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
    create_app_stack
    create_systemd_service
    start_services
    install_victron
    show_summary

    info "Deployment completed successfully at $(date)"
}

main "$@"
