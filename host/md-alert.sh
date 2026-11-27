#!/usr/bin/env bash
# What to do when mdadm's monitor sees a software RAID event (disk failed, array degraded,
# a spare missing...). mdadm calls it as: md-alert.sh EVENT MD-DEVICE [COMPONENT-DEVICE].
# Installed by deploy-edge-server.sh with MD_ALERT=true, on a machine that has mdadm arrays
# and no alert target of its own (no MAILADDR or PROGRAM in mdadm.conf).
#
# It writes to the system log (tag md-alert) and to a log file, so the event is not lost.
# OpenMediaVault's own notification settings are untouched.
event="${1:-unknown}"
device="${2:-?}"
component="${3:-}"
LOG="${MD_ALERT_LOG:-/var/log/md-alert.log}"

case "$event" in
    Fail | FailSpare | DegradedArray | DeviceDisappeared) priority=daemon.crit ;;
    SparesMissing | Rebuild* | MoveSpare) priority=daemon.warning ;;
    *) priority=daemon.notice ;;
esac

msg="mdadm event $event on $device${component:+ ($component)}"
if command -v logger > /dev/null 2>&1; then logger -t md-alert -p "$priority" "$msg"; fi
printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$msg" >> "$LOG"
exit 0
