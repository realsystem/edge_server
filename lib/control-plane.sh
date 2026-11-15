# shellcheck shell=bash
# Fleet control plane: signed heartbeats between the nodes of a fleet of two or more.

# Fleet control plane for the bot: this node's signing key, the description of the fleet
# the bot reads, and where the control port gets published. Any problem here turns the
# control plane off with a warning; it never stops the deploy or the rest of the bot.
prepare_control_plane() {
    CONTROL_ACTIVE=false
    local script_dir key="$APP_DIR/node.key" pub tmp version="" out
    script_dir="$EDGE_SCRIPT_DIR"
    if [[ "$CONTROL_PLANE" != "true" || "$FLEET_ACTIVE" != "true" ]]; then
        # not a fleet (any more): the bot must not keep an old description
        rm -f "$APP_DIR/bot-status/fleet.json"
        if [[ -f "$SYSTEMD_DIR/docker.service.d/edge-after-tailscale.conf" ]]; then
            rm -f "$SYSTEMD_DIR/docker.service.d/edge-after-tailscale.conf"
            systemctl daemon-reload || true
        fi
        return 0
    fi
    if ! command -v openssl > /dev/null; then
        warn "openssl is missing: no fleet control plane on this node"
        return 0
    fi
    mkdir -p "$APP_DIR/bot-state" "$APP_DIR/bot-status"
    chmod 700 "$APP_DIR/bot-state"
    chmod 755 "$APP_DIR/bot-status"
    if [[ ! -s "$key" ]]; then
        # Generated here and never copied out; an existing key is never replaced, because
        # the other nodes know the public half.
        tmp=$(umask 077 && mktemp "$key.XXXXXX")
        if ! openssl genpkey -algorithm ed25519 -out "$tmp" 2> /dev/null; then
            rm -f "$tmp"
            warn "Could not create the node key: no fleet control plane on this node"
            return 0
        fi
        chmod 600 "$tmp"
        mv "$tmp" "$key"
        info "Created this node's signing key $key"
    fi
    chmod 600 "$key"
    pub=$(openssl pkey -in "$key" -pubout -outform DER 2> /dev/null | tail -c 32 | base64 -w0) || pub=""
    if [[ -z "$pub" ]]; then
        warn "Could not read the node key $key: no fleet control plane on this node"
        return 0
    fi
    printf '%s\n' "$pub" > "$APP_DIR/node.pub"
    chmod 644 "$APP_DIR/node.pub"
    NODE_PUBLIC_KEY="$pub"

    version=$(git -C "$script_dir" describe --tags --always 2> /dev/null) || version=""
    tmp=$(mktemp)
    if ! out=$(python3 "$FLEET_TOOL" bot-config "$FLEET_FILE" --node "$FLEET_NODE" \
        --out "$tmp" --version "$version"); then
        rm -f "$tmp"
        warn "Could not describe the fleet for the bot: no fleet control plane on this node"
        return 0
    fi
    eval "$out" # CONTROL_PUBLISH='addr addr'
    if [[ -z "${CONTROL_PUBLISH// /}" ]]; then
        rm -f "$tmp"
        warn "No lan address for this node in the fleet file and no Tailscale address: no fleet control plane on this node"
        return 0
    fi
    chmod 644 "$tmp"
    install_if_changed "$tmp" "$APP_DIR/bot-status/fleet.json" 644
    rm -f "$tmp"
    CONTROL_ACTIVE=true
    # The bot's port is published on this node's Tailscale address, which must exist when
    # Docker starts the container at boot: start Docker after tailscaled.
    mkdir -p "$SYSTEMD_DIR/docker.service.d"
    printf '[Unit]\nWants=tailscaled.service\nAfter=tailscaled.service\n' \
        > "$SYSTEMD_DIR/docker.service.d/edge-after-tailscale.conf"
    systemctl daemon-reload || true
    info "Fleet control plane: port $CONTROL_PORT on ${CONTROL_PUBLISH// /, }; this node's public key:"
    info "  $NODE_PUBLIC_KEY"
}
