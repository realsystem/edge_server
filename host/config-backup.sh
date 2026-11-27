#!/usr/bin/env bash
# Backup of this edge server's own configuration (never recordings) to a folder on
# another disk. Installed by deploy-edge-server.sh when CONFIG_BACKUP_DIR is set; run by
# edge-config-backup.timer, or by hand: config-backup.sh DEST [KEEP]
#
#   DEST          folder for the archives (created root-only)
#   KEEP          archives to keep, oldest are removed (default 14)
# Environment:
#   EDGE_SERVER_DIR   the application folder (default /opt/edge-server)
#   EDGE_SOURCE_DIR   where the deploy script was run from (.cam, .telegram may live there)
#   BACKUP_MOUNT      DEST must be on this mount point; stops a missing drive from being
#                     "backed up" onto the system disk. Empty = no check.
#   SYSTEM_PATHS      files/globs outside the app folder to include (default below)
#
# The archive holds the camera logins and the bot token: it is root-only (0600, folder
# 0700). Without Tailscale's state, fstab and the OMV config you cannot rebuild a node;
# with them and this folder, a deploy brings it back.
set -u

DEST="${1:-}"
KEEP="${2:-14}"
APP="${EDGE_SERVER_DIR:-/opt/edge-server}"
SRC="${EDGE_SOURCE_DIR:-}"
TAG=edge-config-backup

die() {
    echo "$TAG: $*" >&2
    if command -v logger > /dev/null 2>&1; then logger -t "$TAG" "FAILED: $*"; fi
    exit 1
}

[[ "$DEST" == /* ]] || die "usage: $0 DEST [KEEP] (DEST must be an absolute path)"
[[ "$KEEP" =~ ^[0-9]+$ && "$KEEP" -ge 1 ]] || die "KEEP must be a number of 1 or more"
if [[ -n "${BACKUP_MOUNT:-}" ]] && ! mountpoint -q "$BACKUP_MOUNT"; then
    die "$BACKUP_MOUNT is not mounted, not writing to $DEST"
fi

umask 077
mkdir -p "$DEST" || die "cannot create $DEST"
chmod 700 "$DEST"

DEFAULT_SYSTEM_PATHS="/etc/systemd/system/edge-* /etc/systemd/system/mdmonitor.service.d \
/etc/fstab /etc/exports /etc/mdadm/mdadm.conf /etc/openmediavault/config.xml /etc/rc.local \
/root/.docker/config.json /var/lib/tailscale/tailscaled.state"
SYSTEM_PATHS="${SYSTEM_PATHS:-$DEFAULT_SYSTEM_PATHS}"

paths=()
add() { local g; for g in "$@"; do [[ -e "$g" ]] && paths+=("$g"); done; return 0; }
# shellcheck disable=SC2086  # globs are meant to expand
{
    add "$APP"/docker-compose.yml "$APP"/.cam "$APP"/.telegram "$APP"/deploy.env \
        "$APP"/bot-defaults.env "$APP"/frigate/config.yml "$APP"/*.sh "$APP"/smart-report.py \
        "$APP"/*.conf
    [[ -n "$SRC" ]] && add "$SRC"/.cam "$SRC"/.telegram
    add $SYSTEM_PATHS
}
((${#paths[@]} > 0)) || die "nothing to back up (is $APP the application folder?)"

host=$(hostname -s 2> /dev/null || echo host)
out="$DEST/config-${host}-$(date +%Y%m%d-%H%M%S).tar.gz"
if tar -czf "$out.tmp" "${paths[@]}" 2> /dev/null; then
    mv "$out.tmp" "$out" && chmod 600 "$out"
else
    rm -f "$out.tmp"
    die "tar failed"
fi

# keep the newest KEEP archives of this host
# shellcheck disable=SC2012  # file names are ours: no spaces
ls -1t "$DEST"/config-"${host}"-*.tar.gz 2> /dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
command -v logger > /dev/null 2>&1 \
    && logger -t "$TAG" "ok: $out (${#paths[@]} paths, $(du -h "$out" | cut -f1))"
echo "$out"
