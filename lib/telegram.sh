# shellcheck shell=bash
# Telegram bot: its files and defaults, and the host side of /reboot and the
# service requests (/restart, /logs) it can make.

# Defaults for the bot that do not belong in the secrets file: the Home Assistant to
# check and who this node is. The compose file reads this before .telegram, so a
# setting in .telegram (HA_HOST=, say) still wins. No secrets here.
write_bot_defaults() {
    local tmp host="$HA_HOST" port="$HA_PORT"
    if [[ -n "$host" && ! "$host" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
        warn "HA_HOST '$host' is not an address or host name - no Home Assistant check"
        host=""
    fi
    [[ "$port" =~ ^[0-9]{1,5}$ ]] || port=8123
    tmp=$(mktemp)
    {
        echo "# Written by deploy-edge-server.sh. Settings in .telegram win over these."
        printf 'HA_HOST=%s\nHA_PORT=%s\n' "$host" "$port"
        if [[ -n "$EDGE_NODE" ]]; then
            printf 'EDGE_NODE=%s\n' "$EDGE_NODE"
        fi
        if [[ -n "$EDGE_OPERATORS" ]]; then
            printf 'EDGE_OPERATORS=%s\n' "$EDGE_OPERATORS"
        fi
    } > "$tmp"
    install_if_changed "$tmp" "$APP_DIR/bot-defaults.env" 644
    rm -f "$tmp"
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
    TELEGRAM_REBOOT_ENABLED=false

    local script_dir cred="" candidate token ids totp_secret
    script_dir="$EDGE_SCRIPT_DIR"
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
    if [[ -n "$EDGE_BOT_ID" && "${token%%:*}" != "$EDGE_BOT_ID" ]]; then
        warn "$cred: the token belongs to bot ${token%%:*}, but the fleet file gives node $EDGE_NODE the bot_id $EDGE_BOT_ID"
    fi
    if [[ -z "${ids//[[:space:]]/}" && -n "$EDGE_OPERATORS" ]]; then
        info "TELEGRAM_ALLOWED_IDS is empty in .telegram: using the fleet file's operators ($EDGE_OPERATORS)"
    elif [[ -z "${ids//[[:space:]]/}" ]]; then
        warn "TELEGRAM_ALLOWED_IDS is empty: nobody can use the bot yet. Send /id to it, then add your id to .telegram and redeploy"
    elif [[ ! "$ids" =~ $ids_re ]]; then
        warn "$cred: TELEGRAM_ALLOWED_IDS must be numeric user ids separated by commas - skipping the Telegram bot"
        return 0
    fi
    # Optional authenticator secret for /reboot (base32; spaces, dashes and case ignored)
    totp_secret=$(sed -n 's/^\(OWNER\|REBOOT\)_TOTP_SECRET=//p' "$cred" | tail -n 1 | tr -d '\r[:space:]=-' | tr '[:lower:]' '[:upper:]')
    if [[ -n "$totp_secret" && ! "$totp_secret" =~ ^[A-Z2-7]{16,}$ ]]; then
        warn "$cred: REBOOT_TOTP_SECRET must be base32 (A-Z, 2-7), 16+ characters - skipping the Telegram bot. Make one with: python3 telegram-bot/bot.py --totp-setup"
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
    local mod
    for mod in controlplane.py governance.py assistant.py farm.py peercmd.py; do
        if [[ -f "$script_dir/telegram-bot/$mod" ]]; then
            install_if_changed "$script_dir/telegram-bot/$mod" "$APP_DIR/telegram-bot/$mod" 644
        fi
    done
    install_if_changed "$script_dir/collect-diagnostics.sh" "$APP_DIR/telegram-bot/collect-diagnostics.sh" 755
    # The token is a secret: root-only, and never written into the compose file
    if [[ "$cred" == "$APP_DIR/.telegram" ]]; then
        chmod 600 "$APP_DIR/.telegram"
    else
        install_if_changed "$cred" "$APP_DIR/.telegram" 600
    fi
    write_bot_defaults

    # Only OS tools go into the image (the bot code is mounted), so this is
    # quick after the first build.
    prepare_control_plane
    local build_args=()
    # the signing libraries (cryptography, autonomy-core) go into the image for fleets and
    # wherever /reboot is set up: host actions then run through the policy check and the
    # action gateway, with an audit log in bot-state (Autonomy M5)
    if [[ "$CONTROL_ACTIVE" == "true" || -n "$totp_secret" ]]; then
        build_args=(--build-arg WITH_CRYPTO=true)
    fi
    if docker build -q "${build_args[@]}" -t "$TELEGRAM_IMAGE" "$APP_DIR/telegram-bot" > /dev/null; then
        INSTALL_TELEGRAM_BOT=true
    elif docker image inspect "$TELEGRAM_IMAGE" &> /dev/null; then
        warn "Could not rebuild the Telegram bot image - keeping the existing one"
        INSTALL_TELEGRAM_BOT=true
    else
        warn "Could not build the Telegram bot image (no network?) - skipping the Telegram bot"
    fi

    if [[ "$INSTALL_TELEGRAM_BOT" == "true" ]]; then
        # Written by the host (smart-report.py), mounted read-only into the bot
        mkdir -p "$APP_DIR/bot-status"
        chmod 755 "$APP_DIR/bot-status"
    fi

    if [[ "$INSTALL_TELEGRAM_BOT" == "true" && -n "$totp_secret" ]]; then
        # Root-only drop box: the container writes a request file, a host systemd
        # unit acts on it (see install_reboot_unit).
        mkdir -p "$APP_DIR/bot-requests" "$APP_DIR/bot-state"
        chmod 700 "$APP_DIR/bot-requests" "$APP_DIR/bot-state"
        TELEGRAM_REBOOT_ENABLED=true
    fi
}

# Host side of /reboot. The bot container only creates bot-requests/reboot; this
# path unit notices it and reboots. It refuses within 10 minutes of boot, so a
# repeated request can never become a reboot loop.
install_reboot_unit() {
    local dir="$SYSTEMD_DIR"
    if [[ "$TELEGRAM_REBOOT_ENABLED" != "true" ]]; then
        if [[ -f "$dir/edge-reboot.path" || -f "$dir/edge-reboot.service" ]]; then
            systemctl disable --now edge-reboot.path &> /dev/null || true
            rm -f "$dir/edge-reboot.path" "$dir/edge-reboot.service"
            systemctl daemon-reload
            info "Removed the reboot watcher (no REBOOT_TOTP_SECRET)"
        fi
        return 0
    fi

    info "Installing the Telegram /reboot watcher..."
    mkdir -p "$dir"
    cat > "$dir/edge-reboot.path" << UNIT
[Unit]
Description=Reboot request from the Telegram bot

[Path]
PathExists=$APP_DIR/bot-requests/reboot
Unit=edge-reboot.service

[Install]
WantedBy=multi-user.target
UNIT
    cat > "$dir/edge-reboot.service" << UNIT
[Unit]
Description=Reboot this server on request from the Telegram bot

[Service]
Type=oneshot
# Consume the request first, then refuse if the server booted under 10 minutes ago
ExecStartPre=/bin/rm -f $APP_DIR/bot-requests/reboot
ExecStartPre=/usr/bin/awk '{ exit !(\$\$1 > 600) }' /proc/uptime
ExecStart=/usr/bin/systemctl reboot
UNIT
    chmod 644 "$dir/edge-reboot.path" "$dir/edge-reboot.service"
    # A request left over from before must not reboot the machine the moment the watcher starts
    rm -f "$APP_DIR/bot-requests/reboot"
    systemctl daemon-reload
    systemctl enable --now edge-reboot.path
}

# Host side of /restart and /logs: two path units that run service-request.py when the
# bot drops bot-requests/restart-request or logs-request. That script takes nothing but a
# service name from the stack's compose file, so the bot gets no Docker access.
install_service_requests() {
    local dir="$SYSTEMD_DIR" script_dir kind
    script_dir="$EDGE_SCRIPT_DIR"
    if [[ "$TELEGRAM_REBOOT_ENABLED" != "true" || ! -f "$script_dir/telegram-bot/service-request.py" ]]; then
        local removed=false
        for kind in restart logs; do
            if [[ -f "$dir/edge-$kind.path" || -f "$dir/edge-$kind.service" ]]; then
                systemctl disable --now "edge-$kind.path" &> /dev/null || true
                rm -f "$dir/edge-$kind.path" "$dir/edge-$kind.service"
                removed=true
            fi
        done
        rm -f "$APP_DIR/service-request.py"
        if [[ "$removed" == "true" ]]; then
            systemctl daemon-reload
            info "Removed the /restart and /logs watchers (no REBOOT_TOTP_SECRET)"
        fi
        return 0
    fi

    info "Installing the Telegram /restart and /logs watchers..."
    mkdir -p "$dir"
    install -m 755 "$script_dir/telegram-bot/service-request.py" "$APP_DIR/service-request.py"
    local timeout
    for kind in restart logs; do
        timeout=300
        [[ "$kind" == "logs" ]] && timeout=120
        cat > "$dir/edge-$kind.path" << UNIT
[Unit]
Description=$kind request from the Telegram bot

[Path]
PathExists=$APP_DIR/bot-requests/$kind-request
Unit=edge-$kind.service

[Install]
WantedBy=multi-user.target
UNIT
        cat > "$dir/edge-$kind.service" << UNIT
[Unit]
Description=Telegram bot request: $kind a container of the stack
After=docker.service

[Service]
Type=oneshot
Environment=EDGE_SERVER_DIR=$APP_DIR
TimeoutStartSec=$timeout
ExecStart=$APP_DIR/service-request.py $kind
UNIT
        chmod 644 "$dir/edge-$kind.path" "$dir/edge-$kind.service"
        # a request left over from before must not act the moment the watcher starts
        rm -f "$APP_DIR/bot-requests/$kind-request"
    done
    systemctl daemon-reload
    systemctl enable --now edge-restart.path edge-logs.path
}
