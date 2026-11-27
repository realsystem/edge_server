# shellcheck shell=bash
# Optional host protection, off unless asked for, and skipped where it does not apply:
#
#  CONFIG_BACKUP_DIR=/some/folder   a nightly archive of this server's own configuration
#      (compose file, Frigate config, camera logins, bot token, systemd units, fstab, OMV
#      config, Tailscale state) in that folder, newest CONFIG_BACKUP_KEEP kept. The folder
#      should be on another disk (a RAID, a NAS share): if it is on the system disk the
#      deploy says so, and the backup stops if the drive it is on is not mounted.
#  MD_ALERT=true                    a local alert program for the software RAID monitor
#      (mdmonitor). Only where mdadm arrays exist and mdadm.conf has no MAILADDR/PROGRAM
#      of its own; on OpenMediaVault it fixes a failing mdmonitor when e-mail is off.
#
# Both are saved in deploy.env, so --auto keeps them. Setting one back to empty/false on
# the next deploy removes what it installed. Nothing here ever stops the rest of the deploy.

MDSTAT_FILE="${MDSTAT_FILE:-/proc/mdstat}"        # overridable for tests
MDADM_CONF="${MDADM_CONF:-/etc/mdadm/mdadm.conf}"

# What the backup timer and the monitor override carry, so we only ever remove our own.
readonly PROTECT_MARK="# managed by deploy-edge-server.sh (host protection)"

# Remove a unit file only if it is ours.
protect_remove_if_ours() {
    local file
    for file in "$@"; do
        if [[ -f "$file" ]] && grep -qF "$PROTECT_MARK" "$file"; then
            rm -f "$file"
        fi
    done
}

install_config_backup() {
    local dir="$SYSTEMD_DIR" dest="${CONFIG_BACKUP_DIR:-}" keep="${CONFIG_BACKUP_KEEP:-14}"
    local script_dir="$EDGE_SCRIPT_DIR" mount_point="" dest_dev="" root_dev=""

    if [[ -z "$dest" ]]; then
        if [[ -f "$dir/edge-config-backup.timer" ]] && grep -qF "$PROTECT_MARK" "$dir/edge-config-backup.timer"; then
            systemctl disable --now edge-config-backup.timer &> /dev/null || true
            protect_remove_if_ours "$dir/edge-config-backup.service" "$dir/edge-config-backup.timer"
            rm -f "$APP_DIR/config-backup.sh"
            systemctl daemon-reload &> /dev/null || true
            info "Config backup switched off (CONFIG_BACKUP_DIR is empty); existing archives were left alone"
        fi
        return 0
    fi
    # the path ends up in a systemd unit: plain characters only
    if [[ ! "$dest" =~ ^/[A-Za-z0-9._/@+-]+$ || "$dest" == *..* ]]; then
        warn "CONFIG_BACKUP_DIR '$dest' must be an absolute path of letters, digits and . _ / @ + - : no config backup"
        return 0
    fi
    case "$dest" in
        "$APP_DIR" | "$APP_DIR"/* | /proc/* | /sys/* | /dev/* | /run/*)
            warn "CONFIG_BACKUP_DIR '$dest' is inside the application folder or a system folder: no config backup"
            return 0
            ;;
    esac
    if [[ ! "$keep" =~ ^[0-9]+$ || "$keep" -lt 1 ]]; then
        warn "CONFIG_BACKUP_KEEP '$keep' is not a number of 1 or more: using 14"
        keep=14
    fi
    if [[ ! -f "$script_dir/host/config-backup.sh" ]]; then
        warn "host/config-backup.sh not found next to the deploy script: no config backup"
        return 0
    fi

    # Which mount is the folder on? A folder under a drive that is not mounted would be
    # created on the system disk by mistake, so the script checks that mount every night.
    mount_point=$(findmnt -n -o TARGET --target "$(dirname "$dest")" 2> /dev/null | head -n 1) || mount_point=""
    dest_dev=$(df --output=source "$(dirname "$dest")" 2> /dev/null | tail -n 1) || dest_dev=""
    root_dev=$(df --output=source / 2> /dev/null | tail -n 1) || root_dev=""
    if [[ -n "$dest_dev" && "$dest_dev" == "$root_dev" ]]; then
        warn "CONFIG_BACKUP_DIR $dest is on the system disk: a backup there does not survive that disk failing. Point it at another drive."
    fi
    [[ "$mount_point" == "/" ]] && mount_point=""

    info "Installing the nightly config backup into $dest (keeping $keep)..."
    mkdir -p "$dir" "$APP_DIR"
    install -m 755 "$script_dir/host/config-backup.sh" "$APP_DIR/config-backup.sh"
    cat > "$dir/edge-config-backup.service" << UNIT
$PROTECT_MARK
[Unit]
Description=Back up the edge server configuration to $dest

[Service]
Type=oneshot
Nice=19
Environment=EDGE_SERVER_DIR=$APP_DIR
Environment=EDGE_SOURCE_DIR=$script_dir
Environment=BACKUP_MOUNT=$mount_point
ExecStart=$APP_DIR/config-backup.sh $dest $keep
UNIT
    cat > "$dir/edge-config-backup.timer" << UNIT
$PROTECT_MARK
[Unit]
Description=Back up the edge server configuration every night

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable --now edge-config-backup.timer &> /dev/null || warn "Could not start edge-config-backup.timer"
    # one archive now, so there is something to restore from
    if systemctl start edge-config-backup.service &> /dev/null; then
        success "Config backup installed: $dest, every night at 03:30 (first archive written)"
    else
        warn "Config backup installed, but the first run failed: see 'journalctl -u edge-config-backup'"
    fi
}

# Applicable only with software RAID and nobody told mdadm where to send events.
md_alert_applicable() {
    command -v mdadm &> /dev/null || return 1
    grep -q '^md[0-9]' "$MDSTAT_FILE" 2> /dev/null || return 1
    return 0
}

md_conf_has_target() {
    grep -qE '^[[:space:]]*(MAILADDR|PROGRAM)[[:space:]]+[^[:space:]]+' "$MDADM_CONF" 2> /dev/null
}

install_md_alert() {
    local dir="$SYSTEMD_DIR" script_dir="$EDGE_SCRIPT_DIR" want="${MD_ALERT:-false}" mdadm_bin override
    override="$dir/mdmonitor.service.d/edge-server.conf"

    case "$want" in
        true | false | "") ;;
        *)
            warn "MD_ALERT must be true or false, not '$want': RAID alert left as it is"
            return 0
            ;;
    esac
    if [[ "$want" != "true" ]]; then
        if [[ -f "$override" ]] && grep -qF "$PROTECT_MARK" "$override"; then
            protect_remove_if_ours "$override"
            rmdir "$dir/mdmonitor.service.d" 2> /dev/null || true
            rm -f "$APP_DIR/md-alert.sh"
            systemctl daemon-reload &> /dev/null || true
            systemctl restart mdmonitor &> /dev/null || true
            info "RAID alert switched off (MD_ALERT is not true)"
        fi
        return 0
    fi

    if ! md_alert_applicable; then
        info "MD_ALERT skipped: no software RAID (mdadm array) on this machine"
        return 0
    fi
    if md_conf_has_target; then
        info "MD_ALERT skipped: $MDADM_CONF already has a MAILADDR or PROGRAM for RAID events"
        return 0
    fi
    if [[ ! -f "$script_dir/host/md-alert.sh" ]]; then
        warn "host/md-alert.sh not found next to the deploy script: no RAID alert"
        return 0
    fi
    if ! systemctl cat mdmonitor.service &> /dev/null; then
        warn "No mdmonitor.service on this machine: no RAID alert"
        return 0
    fi

    mdadm_bin=$(command -v mdadm)
    info "Installing the RAID event alert for mdmonitor..."
    mkdir -p "$APP_DIR" "$dir/mdmonitor.service.d"
    install -m 755 "$script_dir/host/md-alert.sh" "$APP_DIR/md-alert.sh"
    cat > "$override" << UNIT
$PROTECT_MARK
[Service]
ExecStart=
ExecStart=$mdadm_bin --monitor --scan --syslog --program=$APP_DIR/md-alert.sh
UNIT
    systemctl daemon-reload
    if systemctl restart mdmonitor &> /dev/null; then
        success "RAID alert installed: events go to the system log (tag md-alert) and /var/log/md-alert.log"
    else
        warn "RAID alert installed, but mdmonitor did not start: see 'systemctl status mdmonitor'"
    fi
}
