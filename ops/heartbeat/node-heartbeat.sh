#!/usr/bin/env bash
# Sends one heartbeat from this host to an outside monitor: an Uptime Kuma push URL, or any other URL. A systemd timer runs
# it every few minutes (see install.sh); you can also run it by hand.
#
#   edge-heartbeat [--dry-run]
#
#   --dry-run   print what would be sent (the push token is hidden) and send nothing
#
# Settings come from the environment or from /etc/edge-heartbeat.env (override the path with EDGE_HEARTBEAT_CONF):
#   HEARTBEAT_URL                    the monitor's address. Nothing is sent unless it starts with http. Keep it secret.
#   HEARTBEAT_NAME                   a label for the JSON body (default: the host name)
#   HEARTBEAT_DISK_PATH              the file system to watch (default /)
#   HEARTBEAT_DISK_DEGRADED_PERCENT  at or above this the host is "degraded" (default 90)
#   HEARTBEAT_DEGRADED_AS            up (default): a degraded host only says so in the message; down: the monitor alerts
#
# For an Uptime Kuma push URL (it contains /api/push/) the request is <url>?status=<up|down>&msg=<summary>, and the query of
# the stored URL is ignored. Any other URL is called exactly as stored. The JSON body is sent either way (a monitor like
# Healthchecks keeps it). A host that stops sending at all is always an alert at the monitor.

set -euo pipefail

CONF="${EDGE_HEARTBEAT_CONF:-/etc/edge-heartbeat.env}"
DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  "") ;;
  -h | --help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
  *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
esac

# Read KEY=value lines without executing the file (a URL has & and ? in it). The environment wins over the file.
setting() {
  local name="$1" default="$2" value
  value="${!name:-}"
  if [ -z "$value" ] && [ -r "$CONF" ]; then
    value="$(grep -m1 "^$name=" "$CONF" | cut -d= -f2- | sed -e "s/^['\"]//" -e "s/['\"]\$//" || true)"
  fi
  printf '%s' "${value:-$default}"
}

URL="$(setting HEARTBEAT_URL "")"
NAME="$(setting HEARTBEAT_NAME "$(hostname 2>/dev/null || echo host)")"
DISK_PATH="$(setting HEARTBEAT_DISK_PATH /)"
DISK_LIMIT="$(setting HEARTBEAT_DISK_DEGRADED_PERCENT 90)"
DEGRADED_AS="$(setting HEARTBEAT_DEGRADED_AS up)"

case "$URL" in
  http://* | https://*) ;;
  *) echo "no heartbeat URL set (HEARTBEAT_URL in $CONF): nothing sent"; exit 0 ;;
esac
case "$DEGRADED_AS" in up | down) ;; *) echo "HEARTBEAT_DEGRADED_AS must be up or down" >&2; exit 2 ;; esac
[[ "$DISK_LIMIT" =~ ^[0-9]+$ ]] || { echo "HEARTBEAT_DISK_DEGRADED_PERCENT must be a whole number" >&2; exit 2; }

# What the host looks like. Each reading is "?" when it cannot be taken, and a missing reading never makes a host degraded.
uptime_s="$(awk '{print int($1)}' /proc/uptime 2>/dev/null || true)"
load1="$(awk '{print $1}' /proc/loadavg 2>/dev/null || true)"
mem_pct="$(awk '/^MemTotal:/ {t=$2} /^MemAvailable:/ {a=$2; have=1} END {if (t > 0 && have) printf "%d", (t - a) * 100 / t}' /proc/meminfo 2>/dev/null || true)"
disk_pct="$(df -P "$DISK_PATH" 2>/dev/null | awk 'NR==2 {gsub("%", "", $5); print $5}' || true)"
uptime_s="${uptime_s:-?}"; load1="${load1:-?}"; mem_pct="${mem_pct:-?}"; disk_pct="${disk_pct:-?}"
# a reading that cannot be a percentage is not a reading
for var in mem_pct disk_pct; do
  if [[ "${!var}" =~ ^[0-9]+$ ]] && [ "${!var}" -gt 100 ]; then printf -v "$var" '?'; fi
done

uptime_text="?"
if [[ "$uptime_s" =~ ^[0-9]+$ ]]; then
  if [ "$uptime_s" -ge 172800 ]; then uptime_text="$((uptime_s / 86400)) d"; else uptime_text="$((uptime_s / 3600)) h"; fi
fi

state="ok"
note=""
if [[ "$disk_pct" =~ ^[0-9]+$ ]] && [ "$disk_pct" -ge "$DISK_LIMIT" ]; then
  state="degraded"
  note=" (limit $DISK_LIMIT%)"
fi
summary="$state | up $uptime_text | disk ${disk_pct}%$note | mem ${mem_pct}% | load $load1"
status="up"
if [ "$state" = degraded ] && [ "$DEGRADED_AS" = down ]; then status="down"; fi

urlencode() {
  local s="$1" i c out=""
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      *) out+="$(printf '%%%02X' "'$c")" ;;
    esac
  done
  printf '%s' "$out"
}

target="$URL"
case "$URL" in
  */api/push/*) target="${URL%%\?*}?status=$status&msg=$(urlencode "$summary")" ;;
esac

safe_name="$(printf '%s' "$NAME" | tr -cd 'A-Za-z0-9._ -')"
json="{\"host\":\"$safe_name\",\"status\":\"$state\",\"uptime_s\":\"$uptime_s\",\"disk_percent\":\"$disk_pct\",\"mem_percent\":\"$mem_pct\",\"load1\":\"$load1\"}"

if [ "$DRY_RUN" -eq 1 ]; then
  masked="$(printf '%s' "$target" | sed -E 's#(/api/push/)[^?/]+#\1<hidden>#')"
  echo "would POST to: $masked"
  echo "summary: $summary"
  echo "body: $json"
  exit 0
fi

curl -fsS -m 20 -o /dev/null -X POST -H 'Content-Type: application/json' --data "$json" "$target"
echo "heartbeat sent ($summary)"
