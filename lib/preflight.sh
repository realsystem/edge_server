# shellcheck shell=bash
# Checks and questions before anything on the machine is changed.

preflight_checks() {
    info "Running pre-flight checks..."

    # Must run as root
    if [[ $EUID -ne 0 ]]; then
        fatal "This script must be run as root (use sudo)"
    fi

    # Ubuntu or Debian (lib/os.sh); anything else stops here, before any change
    os_require
    success "OS: $OS_PRETTY_NAME"

    # Check internet connectivity
    info "Testing internet connectivity..."
    if ! curl -sf --connect-timeout 10 "$(os_archive_url)" > /dev/null 2>&1; then
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

    # Laptop or server: decides the power settings and the power loss monitor (lib/host.sh)
    resolve_host_type

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
