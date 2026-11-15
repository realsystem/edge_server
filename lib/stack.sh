# shellcheck shell=bash
# The application stack: Frigate config, docker-compose.yml (Frigate, Home Assistant,
# Mosquitto, Telegram bot, job processor) and camera discovery.

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

# Sets PROCESSOR_ACTIVE and PROCESSOR_PUBLISH (addresses the port is published on).
# Secrets (session secret, admin password, worker token) are made once and kept in
# $APP_DIR/.processor (root only); the queue lives in $APP_DIR/processor.
prepare_processor() {
    PROCESSOR_ACTIVE=false
    PROCESSOR_PUBLISH=""
    if [[ "$INSTALL_PROCESSOR" != "true" ]]; then
        return 0
    fi
    if [[ "$PROCESSOR_IMAGE" == *__PROCESSOR_TAG__* ]]; then
        warn "No processor image pinned in this release: set PROCESSOR_IMAGE=ghcr.io/realsystem/processor-control-plane:<tag>"
        return 0
    fi
    if [[ -n "$PROCESSOR_BIND" ]]; then
        PROCESSOR_PUBLISH="$PROCESSOR_BIND"
    else
        PROCESSOR_PUBLISH=$(tailscale ip -4 2> /dev/null | head -n 1) || PROCESSOR_PUBLISH=""
    fi
    if [[ -z "$PROCESSOR_PUBLISH" ]]; then
        warn "No Tailscale address yet (sudo tailscale up) and no PROCESSOR_BIND: the job processor is not installed this time"
        return 0
    fi
    mkdir -p "$APP_DIR/processor"
    chown 1000:1000 "$APP_DIR/processor" # the image runs as uid 1000
    chmod 700 "$APP_DIR/processor"
    local secrets="$APP_DIR/.processor" tmp
    if [[ ! -s "$secrets" ]]; then
        tmp=$(umask 077 && mktemp "$secrets.XXXXXX")
        {
            echo "# Made by deploy-edge-server.sh; kept across deploys. Root only."
            echo "SESSION_SECRET=$(openssl rand -hex 32)"
            echo "WORKER_TOKEN=$(openssl rand -hex 32)"
            echo "ADMIN_PASSWORD=$(openssl rand -hex 16)"
        } > "$tmp"
        mv "$tmp" "$secrets"
        info "Created the job processor's secrets in $secrets"
    fi
    chmod 600 "$secrets"
    # the bot reads its API key from this file (filled in by start_services)
    if [[ "$INSTALL_TELEGRAM_BOT" == "true" ]]; then
        touch "$APP_DIR/processor-bot.key"
        chmod 644 "$APP_DIR/processor-bot.key"
    fi
    if [[ -n "${PROCESSOR_REGISTRY_TOKEN:-}" ]]; then
        if printf '%s' "$PROCESSOR_REGISTRY_TOKEN" | docker login ghcr.io -u "${PROCESSOR_REGISTRY_USER:-realsystem}" --password-stdin > /dev/null; then
            info "Logged in to ghcr.io for the processor image"
        else
            warn "Could not log in to ghcr.io with PROCESSOR_REGISTRY_TOKEN"
        fi
    fi
    if [[ "$PROCESSOR_PUBLISH" == 100.* ]]; then
        # published on the Tailscale address, which must exist when Docker starts at boot
        mkdir -p "$SYSTEMD_DIR/docker.service.d"
        printf '[Unit]\nWants=tailscaled.service\nAfter=tailscaled.service\n' \
            > "$SYSTEMD_DIR/docker.service.d/edge-after-tailscale.conf"
        systemctl daemon-reload || true
    fi
    PROCESSOR_ACTIVE=true
    info "Job processor: port $PROCESSOR_PORT on $PROCESSOR_PUBLISH, workers authenticate with $PROCESSOR_WORKER_AUTH"
}

# The bot's API key for the processor: created once the processor answers.
processor_bot_key() {
    local keyfile="$APP_DIR/processor-bot.key" key i
    [[ "${PROCESSOR_ACTIVE:-false}" == "true" && "$INSTALL_TELEGRAM_BOT" == "true" ]] || return 0
    [[ -s "$keyfile" ]] && return 0
    for i in $(seq 1 30); do
        if key=$(docker compose exec -T processor ./server -create-api-key "$PROCESSOR_ADMIN_EMAIL" \
            -api-key-name telegram-bot 2> /dev/null | tail -n 1) && [[ "$key" == pk_* ]]; then
            printf '%s\n' "$key" > "$keyfile"
            chmod 644 "$keyfile"
            info "Created the bot's API key for the job processor"
            return 0
        fi
        sleep 2
    done
    warn "Could not create an API key for the bot: /status will show the processor as refused"
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
        cat << 'EOF'
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"   # Docker keeps container logs forever by default
    max-file: "3"

services:
EOF
        if [[ "$INSTALL_MQTT" == "true" ]]; then
            cat << 'EOF'
  #-----------------------------------------------------------------------------
  # Eclipse Mosquitto MQTT Broker
  #-----------------------------------------------------------------------------
  mosquitto:
    image: eclipse-mosquitto:2
    container_name: mosquitto
    restart: unless-stopped
    logging: *default-logging
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
    logging: *default-logging
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
    image: __FRIGATE_IMAGE__
    container_name: frigate
    restart: unless-stopped
    logging: *default-logging
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
    logging: *default-logging
    init: true
    pid: host
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    sysctls:
      # lets /status ping HA_HOST without the ping binary or NET_RAW: the bot sends
      # echo requests over an unprivileged ICMP socket
      - net.ipv4.ping_group_range=0 2147483647
    env_file:
      - ./bot-defaults.env  # deploy-time defaults (HA_HOST, node name); no secrets
      - ./.telegram         # secrets and your own settings; wins over the line above
    environment:
      - TZ=${TIMEZONE:-America/Los_Angeles}
      - FRIGATE_URL=http://frigate:5000
      - EDGE_SERVER_DIR=/opt/edge-server
      - STORAGE_PATH=/media/frigate
      - STORAGE_LABEL=__STORAGE_MOUNT__
EOF
            # /reboot: the one writable path, a drop box only root on the host can use
            [[ "$TELEGRAM_REBOOT_ENABLED" == "true" ]] && printf '      - REQUESTS_DIR=/requests\n'
            if [[ "${PROCESSOR_ACTIVE:-false}" == "true" ]]; then
                printf '      - PROCESSOR_URL=http://processor:8080\n      - PROCESSOR_API_KEY_FILE=/run/edge/processor.key\n'
            fi
            cat << 'EOF'
    volumes:
      # Only what /status and /diag read. Not the whole app dir: that holds
      # camera logins (.cam), Home Assistant tokens and the MQTT password file.
      - ./telegram-bot:/app:ro
      - ./frigate:/opt/edge-server/frigate:ro
      - ./docker-compose.yml:/opt/edge-server/docker-compose.yml:ro
      - ./bot-status:/status:ro
      - __STORAGE_MOUNT__/frigate:/media/frigate:ro
EOF
            [[ "$TELEGRAM_REBOOT_ENABLED" == "true" ]] && printf '      - ./bot-requests:/requests\n'
            [[ "${PROCESSOR_ACTIVE:-false}" == "true" ]] && printf '      - ./processor-bot.key:/run/edge/processor.key:ro\n'
            # state for governed host actions (gateway journal, audit log); fleets mount it below
            if [[ "$TELEGRAM_REBOOT_ENABLED" == "true" && "$CONTROL_ACTIVE" != "true" ]]; then
                printf '      - ./bot-state:/state\n'
            fi
            if [[ "$CONTROL_ACTIVE" == "true" ]]; then
                # fleet control plane: the signing key (read-only), a small state directory,
                # and the control port published on this node's LAN and Tailscale addresses only
                printf '      - ./node.key:/run/edge/node.key:ro\n      - ./bot-state:/state\n'
                printf '    ports:\n'
                IFS=' ' read -ra _publish <<< "$CONTROL_PUBLISH"
                for _addr in "${_publish[@]}"; do
                    printf '      - "%s:%s:%s"\n' "$_addr" "$CONTROL_PORT" "$CONTROL_PORT"
                done
            fi
            cat << 'EOF'
    tmpfs:
      - /tmp
    mem_limit: 256m
    pids_limit: 128
EOF
        fi
        if [[ "${PROCESSOR_ACTIVE:-false}" == "true" ]]; then
            cat << 'EOF'

  #-----------------------------------------------------------------------------
  # Job processor control plane: the queue that workers (a GPU machine) poll.
  # Published on the Tailscale address only; data in ./processor.
  #-----------------------------------------------------------------------------
  processor:
    image: __PROCESSOR_IMAGE__
    container_name: processor
    restart: unless-stopped
    logging: *default-logging
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    env_file:
      - ./.processor  # SESSION_SECRET, WORKER_TOKEN, ADMIN_PASSWORD
    environment:
      - ENV=production
      - DATABASE_PATH=/data/processor.db
      - ADMIN_EMAIL=__PROCESSOR_ADMIN_EMAIL__
      - WORKER_AUTH=__PROCESSOR_WORKER_AUTH__
    volumes:
      - ./processor:/data
    tmpfs:
      - /tmp
    mem_limit: 256m
    ports:
EOF
            local _addr _paddrs
            IFS=' ' read -ra _paddrs <<< "$PROCESSOR_PUBLISH"
            for _addr in "${_paddrs[@]}"; do
                printf '      - "%s:%s:8080"\n' "$_addr" "$PROCESSOR_PORT"
            done
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
    sed -i "s|__FRIGATE_IMAGE__|${FRIGATE_IMAGE}|g" "$APP_DIR/docker-compose.yml"
    sed -i -e "s|__PROCESSOR_IMAGE__|${PROCESSOR_IMAGE}|g" \
        -e "s|__PROCESSOR_ADMIN_EMAIL__|${PROCESSOR_ADMIN_EMAIL}|g" \
        -e "s|__PROCESSOR_WORKER_AUTH__|${PROCESSOR_WORKER_AUTH}|g" "$APP_DIR/docker-compose.yml"

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

# Find ONVIF cameras and add new ones to the Frigate config, using the logins
# in a .cam file ("IP user:pass" per camera, see README). Skipped when there is no .cam file.
discover_cameras() {
    local script_dir cred_file="" candidate
    script_dir="$EDGE_SCRIPT_DIR"

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
