#!/usr/bin/env bash
# Installs the host heartbeat sender (ops/heartbeat/node-heartbeat.sh) with a systemd timer, on any systemd Linux: a NAS, a
# video server, a VM. Safe to run again: it rewrites the same files and keeps the timer running.
#
#   sudo ./ops/heartbeat/install.sh [--url-file FILE] [--name NAME] [--interval-min N] [--degraded-as up|down]
#                                   [--disk-percent N] [--no-first-beat]
#   sudo ./ops/heartbeat/install.sh --uninstall
#
#   --url-file FILE     read the monitor's URL from this file (best). Without it the URL is read from $HEARTBEAT_URL, or
#                       asked for with the typing hidden. A URL on the command line (--url) works but stays in shell
#                       history, so avoid it: the URL is a secret.
#   --name NAME         label in the message body (default: the host name)
#   --interval-min N    minutes between heartbeats, 1 to 60 (default 5)
#   --degraded-as MODE  up (default): a degraded host (disk full) says so in the message only; down: the monitor alerts
#   --disk-percent N    disk use that counts as degraded (default 90)
#   --no-first-beat     do not send one heartbeat right away
#   --root DIR          install under DIR and skip systemd (for packaging and tests; needs no root)
#
# What it creates: /usr/local/sbin/edge-heartbeat, /etc/edge-heartbeat.env (mode 600, holds the URL), and the systemd units
# edge-heartbeat.service and edge-heartbeat.timer. Look at the last beats with: journalctl -u edge-heartbeat -n 20

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${HEARTBEAT_URL:-}"
URL_FILE=""
NAME=""
INTERVAL=5
DEGRADED_AS=up
DISK_PERCENT=90
FIRST_BEAT=1
UNINSTALL=0
ROOT=""

die() { printf 'ERROR: %s\n' "$1" >&2; shift; for h in "$@"; do printf '    hint: %s\n' "$h" >&2; done; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --url-file) URL_FILE="${2:-}"; shift 2 ;;
    --url) URL="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --interval-min) INTERVAL="${2:-}"; shift 2 ;;
    --degraded-as) DEGRADED_AS="${2:-}"; shift 2 ;;
    --disk-percent) DISK_PERCENT="${2:-}"; shift 2 ;;
    --no-first-beat) FIRST_BEAT=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --root) ROOT="${2:-}"; shift 2 ;;
    -h | --help) sed -n '2,23p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown option: $1" "run with --help" ;;
  esac
done

BIN="$ROOT/usr/local/sbin/edge-heartbeat"
CONF="$ROOT/etc/edge-heartbeat.env"
UNIT_DIR="$ROOT/etc/systemd/system"

if [ -z "$ROOT" ]; then
  [ "$(id -u)" -eq 0 ] || die "run this as root" "sudo ./ops/heartbeat/install.sh"
  command -v systemctl >/dev/null 2>&1 || die "this needs systemd (systemctl was not found)"
fi

if [ "$UNINSTALL" -eq 1 ]; then
  if [ -z "$ROOT" ]; then
    systemctl disable --now edge-heartbeat.timer >/dev/null 2>&1 || true
  fi
  rm -f "$BIN" "$CONF" "$UNIT_DIR/edge-heartbeat.service" "$UNIT_DIR/edge-heartbeat.timer"
  [ -n "$ROOT" ] || systemctl daemon-reload
  echo "removed the heartbeat sender, its settings and its timer"
  exit 0
fi

[[ "$INTERVAL" =~ ^[0-9]+$ ]] && [ "$INTERVAL" -ge 1 ] && [ "$INTERVAL" -le 60 ] || die "--interval-min must be 1 to 60"
[[ "$DISK_PERCENT" =~ ^[0-9]+$ ]] && [ "$DISK_PERCENT" -ge 1 ] && [ "$DISK_PERCENT" -le 100 ] || die "--disk-percent must be 1 to 100"
case "$DEGRADED_AS" in up | down) ;; *) die "--degraded-as must be up or down" ;; esac
if [ -n "$NAME" ] && [[ ! "$NAME" =~ ^[A-Za-z0-9._\ -]+$ ]]; then die "--name may only contain letters, digits, space, . _ -"; fi

if [ -n "$URL_FILE" ]; then
  [ -r "$URL_FILE" ] || die "cannot read $URL_FILE"
  URL="$(head -n1 "$URL_FILE" | tr -d '\r\n')"
elif [ -z "$URL" ]; then
  [ -t 0 ] || die "no URL: give --url-file FILE or set HEARTBEAT_URL" "the URL is a secret, so it is not taken from the command line by default"
  read -r -s -p "Heartbeat URL (typing is hidden): " URL
  echo
fi
case "$URL" in
  http://* | https://*) ;;
  *) die "the heartbeat URL must start with http:// or https://" ;;
esac
[[ ! "$URL" =~ [[:space:]\'\"] ]] || die "the heartbeat URL must not contain spaces or quotes"

install -d -m 755 "$(dirname "$BIN")" "$(dirname "$CONF")" "$UNIT_DIR"
install -m 755 "$HERE/node-heartbeat.sh" "$BIN"
umask 077
{
  printf 'HEARTBEAT_URL=%s\n' "$URL"
  [ -z "$NAME" ] || printf 'HEARTBEAT_NAME=%s\n' "$NAME"
  printf 'HEARTBEAT_DEGRADED_AS=%s\n' "$DEGRADED_AS"
  printf 'HEARTBEAT_DISK_DEGRADED_PERCENT=%s\n' "$DISK_PERCENT"
} >"$CONF"
chmod 600 "$CONF"
umask 022

cat >"$UNIT_DIR/edge-heartbeat.service" <<EOF
[Unit]
Description=Send a heartbeat to the outside monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/edge-heartbeat
Nice=10
EOF
cat >"$UNIT_DIR/edge-heartbeat.timer" <<EOF
[Unit]
Description=Send a heartbeat every $INTERVAL minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=${INTERVAL}min
RandomizedDelaySec=20

[Install]
WantedBy=timers.target
EOF
echo "installed: $BIN, $CONF (mode 600), edge-heartbeat.service and .timer (every $INTERVAL min)"

if [ -n "$ROOT" ]; then
  echo "(--root given: systemd not touched)"
  exit 0
fi
systemctl daemon-reload
systemctl enable --now edge-heartbeat.timer >/dev/null
echo "the timer is running: systemctl list-timers edge-heartbeat.timer"
if [ "$FIRST_BEAT" -eq 1 ]; then
  if systemctl start edge-heartbeat.service; then
    echo "first heartbeat sent"
  else
    echo "WARNING: the first heartbeat failed. See: journalctl -u edge-heartbeat -n 20" >&2
    echo "    hint: check the URL, and that this host can reach the monitor over HTTPS" >&2
  fi
fi
echo "to stop it for maintenance: sudo systemctl stop edge-heartbeat.timer   (then start it again)"
