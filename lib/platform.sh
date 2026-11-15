# shellcheck shell=bash
# Appliance platforms: systems that manage the host's settings themselves.
#
# OpenMediaVault (OMV, a NAS built on Debian) keeps its network, firewall, time zone,
# drive mounts and update policy in its own database and writes the files behind them
# itself. Changing those files from outside is at best undone the next time someone saves
# a setting in the OMV web UI, at worst leaves the UI showing something other than what
# the machine does. So on OMV this deploy adds only its own things (the Docker stack,
# Tailscale, Frigate, the bot, the helper timers) and leaves those settings to OMV:
#
#   not touched on OMV         instead
#   -------------------------  -------------------------------------------------------
#   UFW and the Docker rule    skipped; port 5000 has no login, restrict it in OMV
#     in DOCKER-USER             (the rule would also cut off the user's other containers)
#   /etc/fstab, mounting       skipped; recordings go to a folder on an OMV file system
#   time zone                  skipped (containers still use TIMEZONE)
#   unattended-upgrades        skipped; OMV has Update Management
#   Docker installation        required to exist already (OMV-Extras installs it, and a
#                                second Docker apt source breaks apt update)
#   static IP (initial-setup)  skipped; set it in OMV (Network > Interfaces)
#
# HOST_PLATFORM=generic deploys as plain Debian even on OMV; HOST_PLATFORM=omv forces
# these rules. Empty detects: OMV keeps its database in /etc/openmediavault/config.xml.

OMV_CONFIG_FILE="${OMV_CONFIG_FILE:-/etc/openmediavault/config.xml}"  # overridable for tests

# "omv" or "generic"
host_platform() {
    case "$HOST_PLATFORM" in
        omv | generic) echo "$HOST_PLATFORM" ;;
        "")
            if [[ -f "$OMV_CONFIG_FILE" ]]; then
                echo omv
            else
                echo generic
            fi
            ;;
        *) echo "$HOST_PLATFORM" ;; # invalid: check_platform stops the deploy with the reason
    esac
}

platform_is_omv() { [[ "$(host_platform)" == "omv" ]]; }

# Before anything changes: validate the setting and, on OMV, what the deploy depends on.
check_platform() {
    case "$HOST_PLATFORM" in
        omv | generic | "") ;;
        *) fatal "HOST_PLATFORM must be 'omv' or 'generic', not '$HOST_PLATFORM'" ;;
    esac
    platform_is_omv || return 0
    info "OpenMediaVault detected: its firewall, network, time zone, drive mounts and update settings are left to OMV"
    if ! command -v docker &> /dev/null; then
        fatal "Docker is not installed. On OpenMediaVault install it from the web UI (omv-extras, then Docker) so OMV manages it, and run this again. (HOST_PLATFORM=generic treats this machine as plain Debian and installs Docker itself.)"
    fi
}

# On OMV there is no UUID to mount: ask for a folder on one of OMV's file systems
# (/srv/dev-disk-by-uuid-...). Recordings written to the default /mnt/storage would
# fill the system disk.
choose_omv_storage() {
    local answer
    [[ -z "$EXTERNAL_DRIVE_UUID" ]] \
        || warn "EXTERNAL_DRIVE_UUID is ignored on OpenMediaVault: OMV mounts its drives itself"
    if [[ -z "${EDGE_STORAGE_DIR:-}" && "$BATCH_MODE" != "true" ]]; then
        echo ""
        echo "=== Where Frigate keeps its recordings ==="
        echo "File systems mounted by OpenMediaVault:"
        df -h --output=target,size,avail 2> /dev/null | awk 'NR == 1 || $1 ~ /^\/srv\//' || true
        echo "Use a folder on one of them, e.g. /srv/dev-disk-by-uuid-.../frigate"
        while true; do
            read -rp "Folder for recordings [${STORAGE_MOUNT}]: " answer || answer=""
            if [[ -z "$answer" ]]; then
                break
            elif [[ "$answer" == /* ]]; then
                STORAGE_MOUNT="${answer%/}"
                break
            fi
            echo "Please give an absolute path (starting with /)."
        done
    fi
    case "$STORAGE_MOUNT" in
        /srv/*) info "Recordings: $STORAGE_MOUNT/frigate" ;;
        *) warn "Recordings go to $STORAGE_MOUNT, which is not on an OMV file system (/srv/...): they will fill the system disk. Set EDGE_STORAGE_DIR to a folder on a data drive." ;;
    esac
}
