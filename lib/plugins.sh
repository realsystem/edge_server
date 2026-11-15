# shellcheck shell=bash
# Optional add-ons from plugins/: Victron Smart Shunt and Renogy Rover (Bluetooth),
# the power loss monitor (NUT, laptops only), and --uninstall for all of them.

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
    script_dir="$EDGE_SCRIPT_DIR"

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
    # It watches a laptop battery (lib/host.sh decides what this machine is)
    local has_battery=false
    if host_has_battery; then
        has_battery=true
    fi

    # Check if we should install
    if [[ "$NUT_POWER_INSTALL" != "true" ]]; then
        if [[ "$HOST_TYPE" != "laptop" ]]; then
            info "Server (not a laptop) - skipping power loss monitor (NUT_POWER_INSTALL=true installs it anyway)"
            return 0
        fi
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
    script_dir="$EDGE_SCRIPT_DIR"

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
    script_dir="$EDGE_SCRIPT_DIR"

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

# Make the next --auto run leave an uninstalled add-on alone: its INSTALL flag
# becomes false and its secrets are dropped. Edits single lines of deploy.env;
# save_settings would overwrite every saved value with this run's defaults.
forget_addon_settings() {
    local install_var="$1" name tmp
    shift
    [[ -f "$SETTINGS_FILE" ]] || return 0
    tmp=$(umask 077 && mktemp "$SETTINGS_FILE.XXXXXX")
    {
        # keep every line except the ones for this add-on
        while IFS= read -r line; do
            for name in "$install_var" "$@"; do
                [[ "$line" == "[[ -v $name ]] || $name="* ]] && continue 2
            done
            printf '%s\n' "$line"
        done < "$SETTINGS_FILE"
        printf '[[ -v %s ]] || %s=false\n' "$install_var" "$install_var"
        for name in "$@"; do
            printf '[[ -v %s ]] || %s=%q\n' "$name" "$name" ""
        done
    } > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$SETTINGS_FILE"
}

# uninstall_addon TITLE UNIT PLUGIN FLAG INSTALL_VAR [SECRET_VAR...]
#   FLAG: current value of the *_UNINSTALL variable ("true", "false" or empty = ask)
uninstall_addon() {
    local title="$1" unit="$2" plugin="$3" flag="$4" install_var="$5"
    shift 5
    local plugin_dir remove_config=false

    if [[ ! -f "$SYSTEMD_DIR/$unit" ]]; then
        info "$title is not installed - nothing to remove"
        return 0
    fi
    case "$flag" in
        true) ;;
        false)
            info "Keeping $title"
            return 0
            ;;
        *)
            echo ""
            echo "=== $title ==="
            if ! confirm "Uninstall $title?"; then
                info "Keeping $title"
                return 0
            fi
            ;;
    esac

    if ! plugin_dir=$(find_plugin_dir "$plugin"); then
        warn "$title plugin files not found next to this script - cannot uninstall"
        return 1
    fi

    if [[ "$plugin" != "nut-power" ]]; then
        case "$REMOVE_ADDON_CONFIG" in
            true) remove_config=true ;;
            false) ;;
            *)
                if confirm "Also delete its saved configuration (device key, MQTT password)?"; then
                    remove_config=true
                fi
                ;;
        esac
    fi

    info "Uninstalling $title..."
    if REMOVE_CONFIG="$remove_config" SYSTEMD_DIR="$SYSTEMD_DIR" bash "${plugin_dir}/uninstall.sh"; then
        forget_addon_settings "$install_var" "$@"
        success "$title uninstalled"
    else
        warn "$title uninstall failed - check the output above"
        return 1
    fi
}

uninstall_addons() {
    local failed=0
    info "Uninstalling add-ons (each one is asked about separately)"
    uninstall_addon "Victron Smart Shunt monitor" victron-shunt.service victron-shunt \
        "$VICTRON_UNINSTALL" VICTRON_INSTALL VICTRON_ADDRESS VICTRON_KEY || failed=1
    uninstall_addon "Renogy Rover monitor" renogy-rover.service renogy-rover \
        "$RENOGY_UNINSTALL" RENOGY_INSTALL RENOGY_ADDRESS || failed=1
    uninstall_addon "power loss monitor (NUT)" battery-poller.service nut-power \
        "$NUT_POWER_UNINSTALL" NUT_POWER_INSTALL || failed=1
    info "Add-on uninstall finished"
    return "$failed"
}
