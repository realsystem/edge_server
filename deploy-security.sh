#!/usr/bin/env bash
#===============================================================================
# Security Stack Deployment
# NVR system with Frigate, go2rtc, Mosquitto MQTT for 4x Reolink cameras
#
# Prerequisites: Docker installed (run deploy-edge-server.sh first)
# Usage: sudo ./deploy-security.sh
#===============================================================================

set -euo pipefail
IFS=$'\n\t'

#-------------------------------------------------------------------------------
# CONFIGURATION
#-------------------------------------------------------------------------------

APP_DIR="/opt/edge-server"
STORAGE_PATH="/mnt/storage/frigate"
MIN_DISK_GB=50
LOCAL_SUBNET="${LOCAL_SUBNET:-192.168.0.0/16}"
BATCH_MODE="${BATCH_MODE:-false}"

# Camera configuration - loaded from env or secrets
TZ="${TZ:-America/Los_Angeles}"
REOLINK_USER="${REOLINK_USER:-}"
REOLINK_PASS="${REOLINK_PASS:-}"

# Camera IPs - can be overridden via environment
CAM1_IP="${CAM1_IP:-192.168.1.201}"
CAM2_IP="${CAM2_IP:-192.168.1.202}"
CAM3_IP="${CAM3_IP:-192.168.1.203}"
CAM4_IP="${CAM4_IP:-192.168.1.204}"

declare -A CAMERAS=(
    ["cam1_front"]="$CAM1_IP"
    ["cam2_rear"]="$CAM2_IP"
    ["cam3_side"]="$CAM3_IP"
    ["cam4_gate"]="$CAM4_IP"
)

#-------------------------------------------------------------------------------
# OUTPUT FORMATTING
#-------------------------------------------------------------------------------

header()  { echo ""; echo "==============================================================="; echo "  $*"; echo "==============================================================="; }
info()    { echo "[OK] $*"; }
warn()    { echo "[WARN] $*"; }
error()   { echo "[ERROR] $*"; }
step()    { echo "[..] $*"; }

#-------------------------------------------------------------------------------
# PRE-FLIGHT CHECKS
#-------------------------------------------------------------------------------

load_secrets() {
    local secrets_script="$(dirname "$0")/secrets.sh"

    # Try to load from secrets.sh if available
    if [[ -f "$secrets_script" ]] && [[ -f "$HOME/.edge-server-secrets/secrets.enc" ]]; then
        step "Loading secrets from encrypted storage..."
        if [[ -n "${SECRETS_PASSWORD:-}" ]]; then
            eval "$("$secrets_script" export 2>/dev/null)" || true
        else
            warn "Set SECRETS_PASSWORD env var to load secrets automatically"
            warn "Or run: eval \$(./secrets.sh export)"
        fi
    fi

    # Prompt for missing credentials
    if [[ -z "$REOLINK_USER" ]]; then
        if [[ "$BATCH_MODE" == "true" ]]; then
            error "REOLINK_USER not set and running in batch mode"
            exit 1
        fi
        read -rp "Enter camera username [admin]: " REOLINK_USER
        REOLINK_USER="${REOLINK_USER:-admin}"
    fi

    if [[ -z "$REOLINK_PASS" ]]; then
        if [[ "$BATCH_MODE" == "true" ]]; then
            error "REOLINK_PASS not set and running in batch mode"
            exit 1
        fi
        read -rsp "Enter camera password: " REOLINK_PASS
        echo
        if [[ -z "$REOLINK_PASS" ]]; then
            error "Camera password is required"
            exit 1
        fi
    fi
}

preflight_checks() {
    header "Pre-Flight Checks"

    # Root check
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)"
        exit 1
    fi
    info "Running as root"

    # Load secrets/credentials
    load_secrets

    # Docker check
    if ! command -v docker &> /dev/null; then
        error "Docker not installed. Run deploy-edge-server.sh first"
        exit 1
    fi
    info "Docker installed: $(docker --version | cut -d' ' -f3 | tr -d ',')"

    # Storage directory
    step "Checking storage path: $STORAGE_PATH"
    if [[ ! -d "$STORAGE_PATH" ]]; then
        warn "Creating storage directory..."
        mkdir -p "$STORAGE_PATH"/{recordings,clips}
    fi

    if [[ ! -w "$STORAGE_PATH" ]]; then
        error "Storage path $STORAGE_PATH is not writable"
        exit 1
    fi
    info "Storage path exists and is writable"

    # Disk space check
    local free_gb
    free_gb=$(df -BG "$STORAGE_PATH" | awk 'NR==2 {gsub("G",""); print $4}')
    if [[ "$free_gb" -lt "$MIN_DISK_GB" ]]; then
        error "Insufficient disk space: ${free_gb}GB available, need ${MIN_DISK_GB}GB minimum"
        exit 1
    fi
    info "Disk space: ${free_gb}GB available"

    # Intel GPU check
    if [[ -e /dev/dri/renderD128 ]]; then
        info "Intel GPU detected: /dev/dri/renderD128"
    else
        warn "Intel GPU not found - hardware acceleration will be disabled"
    fi
}

#-------------------------------------------------------------------------------
# CAMERA CONNECTIVITY CHECK
#-------------------------------------------------------------------------------

check_cameras() {
    header "Camera Connectivity Check"

    local all_ok=true

    printf "\n%-20s %-16s %-10s\n" "CAMERA" "IP ADDRESS" "STATUS"
    printf "%-20s %-16s %-10s\n" "--------------------" "----------------" "----------"

    for cam_name in "${!CAMERAS[@]}"; do
        local ip="${CAMERAS[$cam_name]}"
        local status

        if timeout 3 bash -c "echo >/dev/tcp/$ip/554" 2>/dev/null; then
            status="PASS"
        else
            status="FAIL"
            all_ok=false
        fi

        printf "%-20s %-16s %-10s\n" "$cam_name" "$ip" "$status"
    done

    echo

    if [[ "$all_ok" != true ]]; then
        warn "Some cameras are unreachable. They will show offline in Frigate."
        if [[ "$BATCH_MODE" != "true" ]]; then
            read -rp "Continue anyway? [y/N]: " response
            [[ ! "$response" =~ ^[Yy]$ ]] && exit 1
        else
            warn "Batch mode: continuing despite unreachable cameras"
        fi
    else
        info "All cameras responding on RTSP port 554"
    fi
}

#-------------------------------------------------------------------------------
# CREATE CONFIGURATION FILES
#-------------------------------------------------------------------------------

create_configs() {
    header "Creating Configuration Files"

    mkdir -p "$APP_DIR"/{mosquitto/{config,data,log},frigate,ha-config}

    #---------------------------------------------------------------------------
    # Environment file
    #---------------------------------------------------------------------------
    step "Creating .env file..."
    cat > "$APP_DIR/.env" << EOF
# Security Stack Environment
TZ=${TZ}
STORAGE_PATH=${STORAGE_PATH}

# Reolink Camera Credentials
REOLINK_USER=${REOLINK_USER}
REOLINK_PASS=${REOLINK_PASS}

# Camera IPs
CAM1_IP=${CAMERAS[cam1_front]}
CAM2_IP=${CAMERAS[cam2_rear]}
CAM3_IP=${CAMERAS[cam3_side]}
CAM4_IP=${CAMERAS[cam4_gate]}
EOF
    chmod 600 "$APP_DIR/.env"
    info "Created .env"

    #---------------------------------------------------------------------------
    # Mosquitto configuration
    #---------------------------------------------------------------------------
    step "Creating Mosquitto config..."
    cat > "$APP_DIR/mosquitto/config/mosquitto.conf" << 'EOF'
# Mosquitto MQTT Broker - Security Stack
listener 1883
allow_anonymous true

persistence true
persistence_location /mosquitto/data/

log_dest file /mosquitto/log/mosquitto.log
log_dest stdout
log_type error
log_type warning
log_type notice
log_type information

connection_messages true
EOF
    chown -R 1883:1883 "$APP_DIR/mosquitto"
    info "Created mosquitto.conf"

    #---------------------------------------------------------------------------
    # Frigate configuration with go2rtc
    #---------------------------------------------------------------------------
    step "Creating Frigate config..."
    cat > "$APP_DIR/frigate/config.yml" << EOF
# Frigate NVR Configuration - Security Stack
# Documentation: https://docs.frigate.video

mqtt:
  enabled: true
  host: 127.0.0.1
  port: 1883
  topic_prefix: frigate
  stats_interval: 60

database:
  path: /config/frigate.db

#-------------------------------------------------------------------------------
# go2rtc - Stream Management
# Main stream (high-res) for recording, sub stream (low-res) for detection
#-------------------------------------------------------------------------------
go2rtc:
  streams:
    cam1_front:
      - rtsp://${REOLINK_USER}:${REOLINK_PASS}@${CAMERAS[cam1_front]}:554/h264Preview_01_main
    cam1_front_sub:
      - rtsp://${REOLINK_USER}:${REOLINK_PASS}@${CAMERAS[cam1_front]}:554/h264Preview_01_sub

    cam2_rear:
      - rtsp://${REOLINK_USER}:${REOLINK_PASS}@${CAMERAS[cam2_rear]}:554/h264Preview_01_main
    cam2_rear_sub:
      - rtsp://${REOLINK_USER}:${REOLINK_PASS}@${CAMERAS[cam2_rear]}:554/h264Preview_01_sub

    cam3_side:
      - rtsp://${REOLINK_USER}:${REOLINK_PASS}@${CAMERAS[cam3_side]}:554/h264Preview_01_main
    cam3_side_sub:
      - rtsp://${REOLINK_USER}:${REOLINK_PASS}@${CAMERAS[cam3_side]}:554/h264Preview_01_sub

    cam4_gate:
      - rtsp://${REOLINK_USER}:${REOLINK_PASS}@${CAMERAS[cam4_gate]}:554/h264Preview_01_main
    cam4_gate_sub:
      - rtsp://${REOLINK_USER}:${REOLINK_PASS}@${CAMERAS[cam4_gate]}:554/h264Preview_01_sub

#-------------------------------------------------------------------------------
# Hardware Acceleration - Intel VA-API
#-------------------------------------------------------------------------------
ffmpeg:
  hwaccel_args: preset-vaapi

detectors:
  cpu1:
    type: cpu
    num_threads: 4

#-------------------------------------------------------------------------------
# Recording & Retention
#-------------------------------------------------------------------------------
record:
  enabled: true
  retain:
    days: 3
    mode: motion
  events:
    retain:
      default: 14
      mode: motion

snapshots:
  enabled: true
  timestamp: true
  bounding_box: true
  retain:
    default: 14

#-------------------------------------------------------------------------------
# Object Detection Settings
#-------------------------------------------------------------------------------
objects:
  track:
    - person
    - car
    - dog
    - cat
  filters:
    person:
      min_area: 5000
      max_area: 100000
      threshold: 0.7
    car:
      min_area: 10000
      threshold: 0.7
    dog:
      min_area: 2000
      threshold: 0.6
    cat:
      min_area: 1000
      threshold: 0.6

#-------------------------------------------------------------------------------
# Camera Definitions
#-------------------------------------------------------------------------------
cameras:
  cam1_front:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/cam1_front
          roles:
            - record
        - path: rtsp://127.0.0.1:8554/cam1_front_sub
          roles:
            - detect
    detect:
      enabled: true
      width: 640
      height: 360
      fps: 5
    motion:
      mask: []

  cam2_rear:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/cam2_rear
          roles:
            - record
        - path: rtsp://127.0.0.1:8554/cam2_rear_sub
          roles:
            - detect
    detect:
      enabled: true
      width: 640
      height: 360
      fps: 5
    motion:
      mask: []

  cam3_side:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/cam3_side
          roles:
            - record
        - path: rtsp://127.0.0.1:8554/cam3_side_sub
          roles:
            - detect
    detect:
      enabled: true
      width: 640
      height: 360
      fps: 5
    motion:
      mask: []

  cam4_gate:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/cam4_gate
          roles:
            - record
        - path: rtsp://127.0.0.1:8554/cam4_gate_sub
          roles:
            - detect
    detect:
      enabled: true
      width: 640
      height: 360
      fps: 5
    motion:
      mask: []
EOF
    info "Created frigate/config.yml"

    #---------------------------------------------------------------------------
    # Docker Compose
    #---------------------------------------------------------------------------
    step "Creating docker-compose.yml..."
    cat > "$APP_DIR/docker-compose.yml" << 'EOF'
services:
  #-----------------------------------------------------------------------------
  # Mosquitto MQTT Broker
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
    healthcheck:
      test: ["CMD", "mosquitto_sub", "-t", "$$SYS/#", "-C", "1", "-i", "healthcheck", "-W", "3"]
      interval: 30s
      timeout: 10s
      retries: 3

  #-----------------------------------------------------------------------------
  # Frigate NVR with go2rtc
  #-----------------------------------------------------------------------------
  frigate:
    image: ghcr.io/blakeblackshear/frigate:stable
    container_name: frigate
    restart: unless-stopped
    privileged: true
    shm_size: "128mb"
    network_mode: host
    volumes:
      - ./frigate/config.yml:/config/config.yml:ro
      - ./frigate:/config
      - ${STORAGE_PATH:-/mnt/storage/frigate}:/media/frigate
      - /etc/localtime:/etc/localtime:ro
      - type: tmpfs
        target: /tmp/cache
        tmpfs:
          size: 1073741824
    devices:
      - /dev/dri/renderD128:/dev/dri/renderD128
    environment:
      - FRIGATE_RTSP_PASSWORD=${REOLINK_PASS:-password}
    depends_on:
      mosquitto:
        condition: service_healthy

  #-----------------------------------------------------------------------------
  # Home Assistant
  #-----------------------------------------------------------------------------
  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:stable
    container_name: homeassistant
    restart: unless-stopped
    network_mode: host
    privileged: true
    volumes:
      - ./ha-config:/config
      - /etc/localtime:/etc/localtime:ro
    environment:
      - TZ=${TZ:-America/Los_Angeles}
    depends_on:
      - mosquitto
      - frigate
EOF
    info "Created docker-compose.yml"

    #---------------------------------------------------------------------------
    # Home Assistant initial config
    #---------------------------------------------------------------------------
    if [[ ! -f "$APP_DIR/ha-config/configuration.yaml" ]]; then
        step "Creating Home Assistant config..."
        cat > "$APP_DIR/ha-config/configuration.yaml" << 'EOF'
homeassistant:
  name: Security
  unit_system: imperial
  time_zone: America/Los_Angeles

default_config:

http:
  server_port: 8123
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
    - 100.64.0.0/10

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
        info "Created ha-config/configuration.yaml"
    else
        info "Home Assistant config already exists, skipping"
    fi
}

#-------------------------------------------------------------------------------
# FIREWALL CONFIGURATION
#-------------------------------------------------------------------------------

configure_firewall() {
    header "Configuring Firewall"

    if ! command -v ufw &> /dev/null; then
        step "Installing UFW..."
        apt-get update -qq && apt-get install -y -qq ufw
    fi

    step "Configuring UFW rules..."

    # Reset and set defaults
    ufw --force reset > /dev/null
    ufw default deny incoming
    ufw default allow outgoing

    # SSH (allow from anywhere for remote access)
    ufw allow 22/tcp comment 'SSH'
    info "Allow SSH (22) from anywhere"

    # MQTT (local subnet only)
    ufw allow from "$LOCAL_SUBNET" to any port 1883 proto tcp comment 'MQTT'
    info "Allow MQTT (1883) from $LOCAL_SUBNET"

    # Frigate Web UI (local subnet only)
    ufw allow from "$LOCAL_SUBNET" to any port 5000 proto tcp comment 'Frigate Web'
    info "Allow Frigate Web (5000) from $LOCAL_SUBNET"

    # RTSP Restream (local subnet only)
    ufw allow from "$LOCAL_SUBNET" to any port 8554 proto tcp comment 'RTSP Restream'
    ufw allow from "$LOCAL_SUBNET" to any port 8554 proto udp comment 'RTSP Restream UDP'
    info "Allow RTSP Restream (8554) from $LOCAL_SUBNET"

    # WebRTC (local subnet only)
    ufw allow from "$LOCAL_SUBNET" to any port 8555 proto tcp comment 'WebRTC TCP'
    ufw allow from "$LOCAL_SUBNET" to any port 8555 proto udp comment 'WebRTC UDP'
    info "Allow WebRTC (8555) from $LOCAL_SUBNET"

    # Home Assistant (local subnet only)
    ufw allow from "$LOCAL_SUBNET" to any port 8123 proto tcp comment 'Home Assistant'
    info "Allow Home Assistant (8123) from $LOCAL_SUBNET"

    # Tailscale (all traffic on tailscale interface)
    ufw allow in on tailscale0 comment 'Tailscale'
    info "Allow Tailscale interface"

    # Enable
    ufw --force enable
    info "Firewall enabled"
}

#-------------------------------------------------------------------------------
# SYSTEMD SERVICE
#-------------------------------------------------------------------------------

create_systemd_service() {
    header "Creating Systemd Service"

    cat > /etc/systemd/system/security-stack.service << EOF
[Unit]
Description=Security NVR Stack (Frigate + MQTT + HA)
Documentation=https://docs.frigate.video
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
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

    systemctl daemon-reload
    systemctl enable security-stack.service
    info "Created and enabled security-stack.service"
}

#-------------------------------------------------------------------------------
# DEPLOY STACK
#-------------------------------------------------------------------------------

deploy_stack() {
    header "Deploying Docker Stack"

    cd "$APP_DIR"

    step "Pulling container images..."
    if ! docker compose pull; then
        error "Failed to pull container images"
        exit 1
    fi

    step "Starting containers..."
    if ! docker compose up -d; then
        error "Failed to start containers"
        error "Check logs: docker compose logs"
        exit 1
    fi

    # Wait for containers
    step "Waiting for services to initialize..."
    sleep 15

    # Verify containers are running
    local failed=false
    for container in mosquitto frigate homeassistant; do
        status=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
        if [[ "$status" != "running" ]]; then
            error "Container $container is $status"
            failed=true
        fi
    done

    if [[ "$failed" == "true" ]]; then
        error "Some containers failed to start"
        docker compose logs --tail=50
        exit 1
    fi

    # Show status
    echo
    docker compose ps
}

#-------------------------------------------------------------------------------
# VALIDATION & SUMMARY
#-------------------------------------------------------------------------------

show_summary() {
    header "Deployment Complete"

    local ip_addr tailscale_ip
    ip_addr=$(hostname -I | awk '{print $1}')
    tailscale_ip=$(tailscale ip -4 2>/dev/null || echo "Not connected")

    echo
    printf "%-25s %-40s\n" "SERVICE" "ACCESS URL"
    printf "%-25s %-40s\n" "-------------------------" "----------------------------------------"
    printf "%-25s %-40s\n" "Frigate NVR" "http://${ip_addr}:5000"
    printf "%-25s %-40s\n" "Home Assistant" "http://${ip_addr}:8123"
    printf "%-25s %-40s\n" "MQTT Broker" "${ip_addr}:1883"
    printf "%-25s %-40s\n" "RTSP Restream" "rtsp://${ip_addr}:8554/<camera>"
    echo

    printf "%-25s %-40s\n" "NETWORK" "ADDRESS"
    printf "%-25s %-40s\n" "-------------------------" "----------------------------------------"
    printf "%-25s %-40s\n" "Local IP" "$ip_addr"
    printf "%-25s %-40s\n" "Tailscale IP" "$tailscale_ip"
    echo

    printf "%-25s %-40s\n" "CAMERA" "RTSP STREAM URL"
    printf "%-25s %-40s\n" "-------------------------" "----------------------------------------"
    for cam_name in "${!CAMERAS[@]}"; do
        printf "%-25s %-40s\n" "$cam_name" "rtsp://${ip_addr}:8554/${cam_name}"
    done
    echo

    echo "Configuration Files:"
    echo "  - Docker Compose:  ${APP_DIR}/docker-compose.yml"
    echo "  - Frigate Config:  ${APP_DIR}/frigate/config.yml"
    echo "  - Environment:     ${APP_DIR}/.env"
    echo "  - HA Config:       ${APP_DIR}/ha-config/configuration.yaml"
    echo

    echo "Storage:"
    echo "  - Recordings:      ${STORAGE_PATH}/recordings"
    echo "  - Clips:           ${STORAGE_PATH}/clips"
    echo

    echo "Management Commands:"
    echo "  - View logs:       cd ${APP_DIR} && docker compose logs -f"
    echo "  - Frigate logs:    docker logs -f frigate"
    echo "  - Restart stack:   sudo systemctl restart security-stack"
    echo "  - Stack status:    sudo systemctl status security-stack"
    echo "  - Update images:   cd ${APP_DIR} && docker compose pull && docker compose up -d"
    echo

    echo "Validation Commands:"
    echo "  - Check Frigate:   curl -s http://localhost:5000/api/stats | jq ."
    echo "  - Check MQTT:      mosquitto_sub -h localhost -t 'frigate/#' -v"
    echo "  - Check cameras:   curl -s http://localhost:5000/api/stats | jq '.cameras'"
    echo
}

#-------------------------------------------------------------------------------
# MAIN
#-------------------------------------------------------------------------------

cleanup_on_error() {
    error "Deployment failed"
    if [[ -d "$APP_DIR" ]] && command -v docker &>/dev/null; then
        warn "Stopping any started containers..."
        cd "$APP_DIR" 2>/dev/null && docker compose down 2>/dev/null || true
    fi
}

main() {
    echo ""
    echo "==============================================================="
    echo "  Security Stack Deployment"
    echo "  Frigate NVR / go2rtc / Mosquitto / Home Assistant"
    echo "==============================================================="
    echo ""

    # Set up cleanup trap
    trap cleanup_on_error ERR

    preflight_checks
    check_cameras
    create_configs
    configure_firewall
    create_systemd_service
    deploy_stack
    show_summary

    info "Deployment completed successfully!"
}

main "$@"
