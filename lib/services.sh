# shellcheck shell=bash
# systemd units on the host: the stack itself, its health check and the helpers
# (firewall for Docker ports, image cleanup, SMART, snapshots); starting the stack;
# and the summary at the end of a deploy.

# Host-side helpers, each a small script plus a systemd unit:
#  - edge-docker-firewall: Docker publishes ports around UFW, so the "allow from
#    the local network" rules do not apply to them. This puts the same rule in
#    Docker's own DOCKER-USER chain.
#  - edge-docker-clean (weekly): remove images nothing uses, so updates do not
#    slowly fill the disk.
#  - edge-smart (every 6 h): disk health for the Telegram bot (it cannot read
#    SMART from inside its locked-down container).
#  - edge-snapshot (every 10 min): firewall rules, open ports, SSH settings and
#    service state for /diag, for the same reason.
install_host_helpers() {
    local dir="$SYSTEMD_DIR" script_dir containers
    script_dir="$EDGE_SCRIPT_DIR"
    containers=$(stack_containers)
    mkdir -p "$dir" "$APP_DIR"
    resolve_local_subnet
    info "Installing the Docker firewall, image cleanup and disk health timers..."

    cat > "$APP_DIR/docker-firewall.sh" << 'SCRIPT'
#!/usr/bin/env bash
# Only the local network and Tailscale may reach the ports Docker publishes
# (Frigate's web UI and RTSP restream have no login on these ports).
set -euo pipefail
LOCAL_SUBNET="__LOCAL_SUBNET__"
CHAIN=EDGE-DOCKER-INPUT

iptables -w -N DOCKER-USER 2> /dev/null || true # Docker creates it; harmless if it exists
iptables -w -N "$CHAIN" 2> /dev/null || true
iptables -w -F "$CHAIN"
iptables -w -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -w -A "$CHAIN" -s "$LOCAL_SUBNET" -j RETURN
iptables -w -A "$CHAIN" -i tailscale0 -j RETURN
iptables -w -A "$CHAIN" -i docker0 -j RETURN   # containers talking out and to each other
iptables -w -A "$CHAIN" -i br+ -j RETURN
iptables -w -A "$CHAIN" -j DROP
iptables -w -C DOCKER-USER -j "$CHAIN" 2> /dev/null || iptables -w -I DOCKER-USER 1 -j "$CHAIN"
SCRIPT
    sed -i "s|__LOCAL_SUBNET__|${LOCAL_SUBNET}|" "$APP_DIR/docker-firewall.sh"
    chmod 700 "$APP_DIR/docker-firewall.sh"

    cat > "$dir/edge-docker-firewall.service" << UNIT
[Unit]
Description=Restrict Docker published ports to the local network and Tailscale
After=docker.service
Requires=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$APP_DIR/docker-firewall.sh

[Install]
WantedBy=multi-user.target
UNIT

    cat > "$APP_DIR/docker-clean.sh" << 'SCRIPT'
#!/usr/bin/env bash
# Remove images nothing uses (old Frigate versions after an upgrade).
# Only while the whole stack is running, so a stopped container never loses its image.
set -euo pipefail
for c in __CONTAINERS__; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "$c" 2> /dev/null)" != "true" ]]; then
        echo "$c is not running - skipping cleanup"
        exit 0
    fi
done
docker image prune -af --filter "until=336h"
docker builder prune -f --filter "until=168h"
SCRIPT
    sed -i "s|__CONTAINERS__|${containers}|" "$APP_DIR/docker-clean.sh"
    chmod 700 "$APP_DIR/docker-clean.sh"

    cat > "$dir/edge-docker-clean.service" << UNIT
[Unit]
Description=Remove unused Docker images
After=docker.service

[Service]
Type=oneshot
Nice=19
ExecStart=$APP_DIR/docker-clean.sh
UNIT
    cat > "$dir/edge-docker-clean.timer" << 'UNIT'
[Unit]
Description=Remove unused Docker images weekly

[Timer]
OnCalendar=Sun *-*-* 04:30:00
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

    local smart=false
    if [[ "$INSTALL_TELEGRAM_BOT" == "true" && -f "$script_dir/telegram-bot/smart-report.py" ]]; then
        smart=true
        install -m 755 "$script_dir/telegram-bot/smart-report.py" "$APP_DIR/smart-report.py"
        mkdir -p "$APP_DIR/bot-status"
        chmod 755 "$APP_DIR/bot-status"
        cat > "$dir/edge-smart.service" << UNIT
[Unit]
Description=Write disk health for the Telegram bot
After=local-fs.target

[Service]
Type=oneshot
Nice=19
ExecStart=$APP_DIR/smart-report.py $APP_DIR/bot-status/disks.json
UNIT
        cat > "$dir/edge-smart.timer" << 'UNIT'
[Unit]
Description=Write disk health every 6 hours

[Timer]
OnBootSec=3min
OnUnitActiveSec=6h
AccuracySec=5min

[Install]
WantedBy=timers.target
UNIT
    else
        if [[ -f "$dir/edge-smart.timer" ]]; then
            systemctl disable --now edge-smart.timer &> /dev/null || true
        fi
        rm -f "$dir/edge-smart.service" "$dir/edge-smart.timer" "$APP_DIR/smart-report.py"
    fi

    local snapshot=false
    if [[ "$INSTALL_TELEGRAM_BOT" == "true" && -f "$script_dir/telegram-bot/host-snapshot.sh" ]]; then
        snapshot=true
        install -m 755 "$script_dir/telegram-bot/host-snapshot.sh" "$APP_DIR/host-snapshot.sh"
        mkdir -p "$APP_DIR/bot-status"
        chmod 755 "$APP_DIR/bot-status"
        cat > "$dir/edge-snapshot.service" << UNIT
[Unit]
Description=Write a network and firewall snapshot for the Telegram bot

[Service]
Type=oneshot
Nice=19
ExecStart=$APP_DIR/host-snapshot.sh $APP_DIR/bot-status/host-snapshot.txt
UNIT
        cat > "$dir/edge-snapshot.timer" << 'UNIT'
[Unit]
Description=Write the network and firewall snapshot every 10 minutes

[Timer]
OnBootSec=4min
OnUnitActiveSec=10min
AccuracySec=1min

[Install]
WantedBy=timers.target
UNIT
    else
        if [[ -f "$dir/edge-snapshot.timer" ]]; then
            systemctl disable --now edge-snapshot.timer &> /dev/null || true
        fi
        rm -f "$dir/edge-snapshot.service" "$dir/edge-snapshot.timer" "$APP_DIR/host-snapshot.sh"
    fi

    chmod 644 "$dir"/edge-docker-*.service "$dir"/edge-docker-clean.timer
    systemctl daemon-reload
    # restart, not just "enable --now": the unit stays active after it has run once, so
    # without this a changed LOCAL_SUBNET would not reach Docker's chain until a reboot
    systemctl enable edge-docker-firewall.service
    systemctl restart edge-docker-firewall.service
    systemctl enable --now edge-docker-clean.timer
    if [[ "$smart" == "true" ]]; then
        chmod 644 "$dir/edge-smart.service" "$dir/edge-smart.timer"
        systemctl enable --now edge-smart.timer
        systemctl start --no-block edge-smart.service || true # first report now, not in 3 minutes
    fi
    if [[ "$snapshot" == "true" ]]; then
        chmod 644 "$dir/edge-snapshot.service" "$dir/edge-snapshot.timer"
        systemctl enable --now edge-snapshot.timer
        systemctl start --no-block edge-snapshot.service || true
    fi
}

# Names of the containers this deployment runs
stack_containers() {
    local containers="frigate"
    [[ "$INSTALL_MQTT" == "true" ]] && containers="$containers mosquitto"
    [[ "$INSTALL_HOMEASSISTANT" == "true" ]] && containers="$containers homeassistant"
    [[ "$INSTALL_TELEGRAM_BOT" == "true" ]] && containers="$containers telegram-bot"
    [[ "${PROCESSOR_ACTIVE:-false}" == "true" ]] && containers="$containers processor"
    echo "$containers"
}

create_systemd_service() {
    info "Creating systemd service for auto-start..."

    cat > /etc/systemd/system/edge-server.service << EOF
[Unit]
Description=Edge Server Docker Compose Stack
Documentation=https://docs.frigate.video
After=network-online.target docker.service edge-docker-firewall.service
Wants=network-online.target edge-docker-firewall.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${APP_DIR}
# No "compose pull" here: after a reboot the stack must start without internet.
# Images are pulled when you run the deploy script.
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

    local containers
    containers=$(stack_containers)
    sed -i -e "s|__APP_DIR__|${APP_DIR}|" -e "s|__STORAGE_MOUNT__|${STORAGE_MOUNT}|" \
        -e "s|__CONTAINERS__|${containers}|" "$APP_DIR/health-check.sh"
    chmod +x "$APP_DIR/health-check.sh"

    # Reload and enable services
    systemctl daemon-reload
    systemctl enable edge-server.service
    systemctl enable edge-server-health.timer

    success "Systemd services created and enabled"
}

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

    processor_bot_key

    # Wait for containers to start
    sleep 10

    # Show status
    docker compose ps

    # Start health check timer
    systemctl start edge-server-health.timer

    success "Services started"
}

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
    if [[ "$CONTROL_ACTIVE" == "true" ]]; then
        echo
        echo "Fleet: this node (${FLEET_NODE}) signs its messages with a key made on this machine."
        echo "  public key: ${NODE_PUBLIC_KEY}"
        echo "  Put it under nodes.${FLEET_NODE}.public_key in fleet.yaml, copy the file to every"
        echo "  node and deploy again (or /redeploy) so the others accept this node's messages."
        echo "  /fleet in Telegram shows who has been heard from."
    fi
    if [[ "${PROCESSOR_ACTIVE:-false}" == "true" ]]; then
        echo
        echo "Job processor: http://${PROCESSOR_PUBLISH%% *}:${PROCESSOR_PORT} (web UI login ${PROCESSOR_ADMIN_EMAIL}; password in ${APP_DIR}/.processor)"
        echo "  Add a GPU worker (README: Job processor). The worker logs its public key; register it here:"
        echo "    cd ${APP_DIR} && docker compose exec processor ./server -register-worker-key gpu-1=<key> -capabilities llm.chat"
        echo "  API key for another client:"
        echo "    docker compose exec processor ./server -create-api-key ${PROCESSOR_ADMIN_EMAIL} -api-key-name <name>"
    fi
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
