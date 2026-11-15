# shellcheck shell=bash
# What kind of machine this is, and the settings that depend on it.
#
# HOST_TYPE=laptop: closing the lid does nothing, sleep/suspend/hibernate are switched
#   off (an NVR has to stay up), and the power loss monitor (NUT, which watches the
#   laptop's battery) is offered.
# HOST_TYPE=server: a NAS (OpenMediaVault, say), a mini PC or a VM. Its lid, sleep and
#   power settings are left exactly as they are; they belong to its owner.
#
# Empty HOST_TYPE asks on a manual run, offering the type detected from the hardware;
# batch runs (--auto, BATCH_MODE=true) take the detected type. The answer is saved in
# deploy.env, so --auto keeps it.

POWER_SUPPLY_DIR="${POWER_SUPPLY_DIR:-/sys/class/power_supply}"  # overridable for tests
DMI_CHASSIS_FILE="${DMI_CHASSIS_FILE:-/sys/class/dmi/id/chassis_type}"
LOGIND_CONF="${LOGIND_CONF:-/etc/systemd/logind.conf}"

# A battery in /sys/class/power_supply (BAT0, BAT1, ...)
host_has_battery() {
    local supply
    for supply in "$POWER_SUPPLY_DIR"/BAT*; do
        [[ -d "$supply" ]] && return 0
    done
    return 1
}

# "laptop" or "server": the SMBIOS chassis type says portable/laptop/notebook/
# sub-notebook/convertible/detachable, or there is a battery.
detect_host_type() {
    local chassis=""
    chassis=$(tr -d '[:space:]' < "$DMI_CHASSIS_FILE" 2> /dev/null) || chassis=""
    case "$chassis" in
        8 | 9 | 10 | 14 | 31 | 32)
            echo laptop
            return 0
            ;;
    esac
    if host_has_battery; then
        echo laptop
    else
        echo server
    fi
}

# Make HOST_TYPE laptop or server: keep a valid setting, else ask (manual run) or detect.
resolve_host_type() {
    local detected answer
    case "$HOST_TYPE" in
        laptop | server)
            info "Machine type: $HOST_TYPE (set by HOST_TYPE)"
            return 0
            ;;
        "") ;;
        *) fatal "HOST_TYPE must be 'laptop' or 'server', not '$HOST_TYPE'" ;;
    esac
    detected=$(detect_host_type)
    if [[ "$BATCH_MODE" == "true" ]]; then
        HOST_TYPE="$detected"
        info "Machine type: $HOST_TYPE (detected; set HOST_TYPE to change it)"
        return 0
    fi

    echo ""
    echo "=== Machine type ==="
    echo "  laptop: lid close is ignored, sleep/suspend are switched off, and the power"
    echo "          loss monitor is offered (it shuts down when the battery runs low)"
    echo "  server: a NAS, mini PC or VM; its power settings are left as they are"
    echo ""
    while true; do
        if ! read -rp "Is this machine a laptop or a server? [${detected}]: " answer; then
            answer=""  # no terminal: take the detected type
            echo
        fi
        case "${answer,,}" in
            "") HOST_TYPE="$detected" ;;
            l | laptop) HOST_TYPE=laptop ;;
            s | server) HOST_TYPE=server ;;
            *)
                echo "Please answer laptop or server."
                continue
                ;;
        esac
        break
    done
    info "Machine type: $HOST_TYPE"
}

#-------------------------------------------------------------------------------
# SYSTEM SETTINGS
#-------------------------------------------------------------------------------

configure_system() {
    info "Configuring system settings..."

    # Set timezone (OpenMediaVault keeps its own and would change it back)
    if platform_is_omv; then
        info "OpenMediaVault: system time zone left to OMV (the containers use $TIMEZONE)"
    else
        timedatectl set-timezone "$TIMEZONE" || true
    fi

    if [[ "$HOST_TYPE" == "laptop" ]]; then
        configure_laptop_power
    else
        info "Server: lid, sleep and suspend settings left as they are"
    fi
}

# A laptop must keep running with the lid closed and never go to sleep.
configure_laptop_power() {
    local logind_conf="$LOGIND_CONF" setting key
    info "Configuring lid switch handling..."

    # Backup original if not already backed up
    [[ ! -f "${logind_conf}.orig" ]] && cp "$logind_conf" "${logind_conf}.orig"

    # Set lid switch options idempotently
    for setting in "HandleLidSwitch=ignore" "HandleLidSwitchExternalPower=ignore" "HandleLidSwitchDocked=ignore"; do
        key="${setting%%=*}"
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

    success "Laptop power management configured"
}
