# shellcheck shell=bash
# The local network the server sits on, and the UFW firewall built from it.

# Print "NETWORK INTERFACE KIND" for the local network the server sits on: the connected
# network of the ethernet interface (en*, eth*; the one with the default route if there
# are several), else of the default-route interface (KIND "fallback"). Docker bridges,
# Tailscale and the like are never used. Fails when nothing is found.
detect_local_subnet() {
    local IFS=$' \t\n'
    local net dev defdev="" wired_dev="" wired_net="" via_dev="" via_net=""
    defdev=$(ip -4 -o route show default 2> /dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')
    while read -r net _ dev _; do
        [[ "$net" =~ ^[0-9]+(\.[0-9]+){3}/[0-9]+$ && -n "$dev" ]] || continue
        if [[ "$dev" =~ ^(en|eth) ]]; then
            if [[ -z "$wired_dev" || "$dev" == "$defdev" && "$wired_dev" != "$defdev" ]]; then
                wired_dev=$dev
                wired_net=$net
            fi
        elif [[ -n "$defdev" && "$dev" == "$defdev" && ! "$dev" =~ ^(docker|br-|veth|virbr|tailscale|lo) ]]; then
            via_dev=$dev
            via_net=$net
        fi
    done < <(ip -4 -o route show scope link proto kernel 2> /dev/null)
    if [[ -n "$wired_net" ]]; then
        echo "$wired_net $wired_dev wired"
    elif [[ -n "$via_net" ]]; then
        echo "$via_net $via_dev fallback"
    else
        return 1
    fi
}

# Make LOCAL_SUBNET a valid IPv4 network (CIDR): keep the one that was set, or detect it.
resolve_local_subnet() {
    local IFS=$' \t\n'
    local found net dev kind
    if [[ -z "$LOCAL_SUBNET" ]]; then
        if ! found=$(detect_local_subnet); then
            fatal "Cannot tell the local network from the ethernet interface (is the cable plugged in?). Set it by hand: sudo LOCAL_SUBNET=10.0.0.0/24 ./deploy-edge-server.sh"
        fi
        read -r net dev kind <<< "$found"
        LOCAL_SUBNET=$net
        if [[ "$kind" == "wired" ]]; then
            info "Local network $LOCAL_SUBNET (detected on $dev)"
        else
            warn "No ethernet interface found: using $LOCAL_SUBNET of $dev, which has the default route"
        fi
    else
        info "Local network $LOCAL_SUBNET (set by LOCAL_SUBNET)"
    fi
    [[ "$LOCAL_SUBNET" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]] \
        || fatal "LOCAL_SUBNET '$LOCAL_SUBNET' is not an IPv4 network like 10.0.0.0/24"
}

configure_firewall() {
    if platform_is_omv; then
        warn "OpenMediaVault: UFW is not set up (it can conflict with OMV's firewall settings)"
        warn "Frigate's web UI (port 5000) has no login: if this machine can be reached from outside your LAN, restrict it in OMV (Network > Firewall)"
        return 0
    fi
    resolve_local_subnet
    info "Configuring UFW firewall..."

    # Reset to defaults (idempotent)
    ufw --force reset > /dev/null

    # Default policies
    ufw default deny incoming
    ufw default allow outgoing

    # Allow SSH from anywhere (critical for remote access)
    ufw allow ssh comment 'SSH'

    # Allow services from local subnet only. (Docker publishes its ports around
    # UFW; edge-docker-firewall applies the same rule to them, see install_host_helpers.)
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
