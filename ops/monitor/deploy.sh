#!/usr/bin/env bash
# Deploys the off-site heartbeat monitor (Uptime Kuma, HTTPS through Caddy) on a Debian or Ubuntu VM, for example a
# free GCP e2-micro. Safe to run again: it updates in place and keeps the data.
#
#   sudo ./ops/monitor/deploy.sh [--domain NAME] [--admin-ip CIDR[,CIDR...]] [--watchdog-url URL] [--caddy MODE] [--yes]
#
#   --domain NAME      public host name for the monitor. Default: <your-ip>.sslip.io, or monitor.<your-ip>.sslip.io when
#                      a Caddy already runs here (works with no DNS setup)
#   --admin-ip LIST    only these addresses may open the admin UI; the heartbeat endpoint (/api/push/*) stays
#                      open to everyone, because a site's public address changes (Starlink, mobile data). STRONGLY recommended.
#   --watchdog-url URL a free hosted check (for example from healthchecks.io) that this VM pings every 5 minutes
#                      while the monitor is healthy, so you hear about it if this VM itself dies. Recommended.
#   --caddy MODE       auto (default): use the Caddy service already running on this VM, else start our own
#                      host:   append a marked block to that Caddy's Caddyfile (backup, validate, reload; undone on error)
#                      docker: run our own Caddy container (needs ports 80 and 443 free)
#                      none:   run only Kuma on 127.0.0.1:3001 and print the block for whatever proxy you use
#   --caddyfile PATH   the Caddyfile to append to in host mode (default: the one the caddy service uses)
#   --email ADDR       Let's Encrypt contact for our own Caddy (docker mode only)
#   --image IMAGE      Uptime Kuma image. Default louislam/uptime-kuma:2 (v2 accepts the POST the heartbeat sends)
#   --yes              do not ask for confirmation
#   --print-caddyfile  print the Caddy configuration for the given options and exit (nothing is installed)
#
# Values from an earlier run are kept (/opt/edge-monitor/.env), so updating is just: git pull && sudo ./deploy.sh
# See ops/monitor/README.md for the whole story. Secrets never go in this repository.

set -euo pipefail

APP_DIR=/opt/edge-monitor
BACKUP_DIR=/var/backups/edge-monitor
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BEGIN_MARK="# BEGIN edge-monitor (managed by ops/monitor/deploy.sh: edits between the markers are overwritten)"
END_MARK="# END edge-monitor"

DOMAIN="${DOMAIN:-}"
ACME_EMAIL="${ACME_EMAIL:-}"
ADMIN_IPS="${ADMIN_IPS:-}"
KUMA_IMAGE="${KUMA_IMAGE:-}"
WATCHDOG_URL="${WATCHDOG_URL:-}"
CADDY_MODE="${CADDY_MODE:-auto}"
CADDYFILE_PATH="${CADDYFILE_PATH:-}"
ASSUME_YES=0
PRINT_CADDY=0
BLOCK_ONLY=0

say() { printf '\n==> %s\n' "$*"; }
ok() { printf '    ok: %s\n' "$*"; }
hint() { printf '    hint: %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die() {
  local msg="$1"
  shift
  printf '\nERROR: %s\n' "$msg" >&2
  for h in "$@"; do printf '    hint: %s\n' "$h" >&2; done
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --email) ACME_EMAIL="${2:-}"; shift 2 ;;
    --admin-ip) ADMIN_IPS="${2:-}"; shift 2 ;;
    --watchdog-url) WATCHDOG_URL="${2:-}"; shift 2 ;;
    --caddy) CADDY_MODE="${2:-}"; shift 2 ;;
    --caddyfile) CADDYFILE_PATH="${2:-}"; shift 2 ;;
    --image) KUMA_IMAGE="${2:-}"; shift 2 ;;
    --yes | -y) ASSUME_YES=1; shift ;;
    --print-caddyfile) PRINT_CADDY=1; shift ;;
    --caddy-block-only) BLOCK_ONLY=1; shift ;;
    -h | --help) sed -n '2,24p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown option: $1" "run with --help" ;;
  esac
done

# Settings from a previous run fill in whatever was not given now.
if [ -f "$APP_DIR/.env" ] && [ "$PRINT_CADDY" -eq 0 ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      DOMAIN) [ -n "$DOMAIN" ] || DOMAIN="$value" ;;
      ACME_EMAIL) [ -n "$ACME_EMAIL" ] || ACME_EMAIL="$value" ;;
      ADMIN_IPS) [ -n "$ADMIN_IPS" ] || ADMIN_IPS="$value" ;;
      KUMA_IMAGE) [ -n "$KUMA_IMAGE" ] || KUMA_IMAGE="$value" ;;
      WATCHDOG_URL) [ -n "$WATCHDOG_URL" ] || WATCHDOG_URL="$value" ;;
      CADDY_MODE) [ "$CADDY_MODE" != auto ] || CADDY_MODE="$value" ;;
      CADDYFILE_PATH) [ -n "$CADDYFILE_PATH" ] || CADDYFILE_PATH="$value" ;;
    esac
  done <"$APP_DIR/.env"
fi
KUMA_IMAGE="${KUMA_IMAGE:-louislam/uptime-kuma:2}"

# The site block for the monitor. In docker mode Caddy reaches Kuma by its service name; otherwise on loopback.
site_block() {
  local upstream="127.0.0.1:3001"
  [ "$CADDY_MODE" != docker ] || upstream="kuma:3001"
  printf '%s {\n\tencode gzip\n' "$DOMAIN"
  if [ -n "$ADMIN_IPS" ]; then
    printf '\t# Only the heartbeat endpoint is public; the admin UI is limited to these addresses.\n'
    printf '\t@outside {\n\t\tnot path /api/push/*\n\t\tnot remote_ip %s\n\t}\n' "${ADMIN_IPS//,/ }"
    printf '\trespond @outside "Forbidden" 403\n'
  fi
  printf '\treverse_proxy %s\n}\n' "$upstream"
}

# What our own Caddy container reads (docker mode): the site block plus an optional global email.
caddyfile() {
  if [ -n "$ACME_EMAIL" ]; then
    printf '{\n\temail %s\n}\n\n' "$ACME_EMAIL"
  fi
  site_block
}

check_inputs() {
  [ -z "$DOMAIN" ] || [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "the domain '$DOMAIN' does not look like a host name"
  [ -z "$ACME_EMAIL" ] || [[ "$ACME_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]] || die "the email '$ACME_EMAIL' does not look like an address"
  if [ -n "$ADMIN_IPS" ]; then
    local ip
    IFS=',' read -r -a list <<<"$ADMIN_IPS"
    for ip in "${list[@]}"; do
      [[ "$ip" =~ ^[0-9a-fA-F:.]+(/[0-9]+)?$ ]] || die "'$ip' in --admin-ip is not an address or CIDR" "example: --admin-ip 203.0.113.7/32,198.51.100.0/24"
    done
  fi
  [ -z "$WATCHDOG_URL" ] || [[ "$WATCHDOG_URL" =~ ^https://[^[:space:]]+$ ]] || die "--watchdog-url must be an https:// address" "example: https://hc-ping.com/your-uuid-here"
  [[ "$KUMA_IMAGE" =~ ^[A-Za-z0-9._/:@-]+$ ]] || die "the image name '$KUMA_IMAGE' is not valid"
  case "$CADDY_MODE" in auto | docker | host | none) ;; *) die "--caddy must be auto, docker, host or none, not '$CADDY_MODE'" ;; esac
  [ -z "$CADDYFILE_PATH" ] || [[ "$CADDYFILE_PATH" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "--caddyfile must be an absolute path without spaces"
}

# Existing Caddy: add (or refresh) our marked block. Everything else in the file stays as it was.
apply_caddy_block() {
  say "Adding $DOMAIN to $CADDYFILE_PATH"
  local begins ends
  begins="$(grep -c '^# BEGIN edge-monitor' "$CADDYFILE_PATH" || true)"
  ends="$(grep -c '^# END edge-monitor' "$CADDYFILE_PATH" || true)"
  [ "$begins" = "$ends" ] && [ "$begins" -le 1 ] || die "the markers of an earlier run in $CADDYFILE_PATH are damaged (BEGIN x$begins, END x$ends), so I will not edit it" "fix or remove the '# BEGIN edge-monitor' ... '# END edge-monitor' lines by hand, then run again"
  rest="$(mktemp)"
  sed '/^# BEGIN edge-monitor/,/^# END edge-monitor/d' "$CADDYFILE_PATH" >"$rest"
  pattern="^[[:space:]]*(https?://)?${DOMAIN//./\\.}([[:space:],{:]|\$)"
  if grep -Eq "$pattern" "$rest"; then
    rm -f "$rest"
    die "$CADDYFILE_PATH already has a site for $DOMAIN that is not ours" "pick another name: --domain monitor2.$DOMAIN, or --domain <a name only the monitor uses>"
  fi
  backup="$CADDYFILE_PATH.edge-monitor-backup-$(date +%Y%m%d-%H%M%S)"
  cp -p "$CADDYFILE_PATH" "$backup"
  {
    cat "$rest"
    printf '\n%s\n' "$BEGIN_MARK"
    site_block
    printf '%s\n' "$END_MARK"
  } >"$CADDYFILE_PATH"
  rm -f "$rest"
  if ! out="$(caddy validate --config "$CADDYFILE_PATH" --adapter caddyfile 2>&1)"; then
    cp -p "$backup" "$CADDYFILE_PATH"
    die "Caddy rejected the new configuration, so your Caddyfile was restored unchanged" "$out" "backup kept at $backup"
  fi
  if ! systemctl reload caddy; then
    cp -p "$backup" "$CADDYFILE_PATH"
    systemctl reload caddy || true
    die "Caddy could not reload, so your Caddyfile was restored" "see: sudo journalctl -u caddy -n 50" "backup kept at $backup"
  fi
  ok "Caddy reloaded. Your Caddyfile before this change: $backup"
  hint "to remove the monitor from Caddy later: delete the lines from '# BEGIN edge-monitor' to '# END edge-monitor' and run: sudo systemctl reload caddy"
}

if [ "$PRINT_CADDY" -eq 1 ]; then
  DOMAIN="${DOMAIN:-monitor.example.com}"
  check_inputs
  [ "$CADDY_MODE" != auto ] || CADDY_MODE=docker
  if [ "$CADDY_MODE" = docker ]; then caddyfile; else site_block; fi
  exit 0
fi

if [ "$BLOCK_ONLY" -eq 1 ]; then
  # Re-apply only the Caddyfile block (used by the tests, handy after editing the Caddyfile by hand).
  CADDY_MODE=host
  [ -n "$DOMAIN" ] || die "--caddy-block-only needs --domain"
  [ -n "$CADDYFILE_PATH" ] || die "--caddy-block-only needs --caddyfile"
  check_inputs
  apply_caddy_block
  exit 0
fi

meta() { curl -fsS -m 3 -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1" 2>/dev/null || true; }

say "Checking the machine"
[ "$(id -u)" -eq 0 ] || die "run this as root" "sudo ./ops/monitor/deploy.sh"
[ -r /etc/os-release ] || die "cannot tell which Linux this is"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
  debian | ubuntu) ok "${PRETTY_NAME:-$ID}" ;;
  *) die "this script supports Debian and Ubuntu, found '${ID:-unknown}'" "create the VM from a Debian 12 or Ubuntu 24.04 image" ;;
esac
command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl >/dev/null; }
check_inputs

# Which Caddy? An existing one is respected: we never start a second one on its ports.
if [ "$CADDY_MODE" = auto ]; then
  if systemctl is-active --quiet caddy 2>/dev/null; then
    CADDY_MODE=host
    ok "a Caddy service is already running here: the monitor will be added to its Caddyfile (nothing else in it is touched)"
  elif ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Eq '[:\]](80|443)$'; then
    die "something already listens on port 80 or 443, and it is not a Caddy service" \
      "see what it is: sudo ss -ltnp | grep -E ':80 |:443 '" \
      "if it is Caddy in Docker or another proxy, add a site for the monitor there (proxy to 127.0.0.1:3001) and run: sudo ./ops/monitor/deploy.sh --caddy none  (it prints the block)"
  else
    CADDY_MODE=docker
    ok "no web server on ports 80 and 443: the monitor brings its own Caddy"
  fi
elif [ "$CADDY_MODE" = docker ] && ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Eq '[:\]](80|443)$'; then
  die "--caddy docker needs ports 80 and 443 free, but something listens on them" "use --caddy host (existing Caddy service) or --caddy none"
fi

if [ "$CADDY_MODE" = host ]; then
  command -v caddy >/dev/null 2>&1 || die "the caddy command is not in PATH, so I cannot validate a change to its config" "install Caddy from its apt repository, or use --caddy none"
  if [ -z "$CADDYFILE_PATH" ]; then
    CADDYFILE_PATH="$(systemctl cat caddy 2>/dev/null | sed -nE 's/^ExecStart=.*--config[ =]([^ ]+).*/\1/p' | head -1)"
    CADDYFILE_PATH="${CADDYFILE_PATH:-/etc/caddy/Caddyfile}"
  fi
  [ -w "$CADDYFILE_PATH" ] || die "cannot write the Caddyfile $CADDYFILE_PATH" "pass the right one: --caddyfile /path/to/Caddyfile"
  case "$(head -c 1 "$CADDYFILE_PATH")" in "{") grep -q '^[[:space:]]*"apps"' "$CADDYFILE_PATH" && die "$CADDYFILE_PATH looks like Caddy's JSON config, not a Caddyfile" "use --caddy none and add the site yourself" ;; esac
  begins="$(grep -c '^# BEGIN edge-monitor' "$CADDYFILE_PATH" || true)"
  ends="$(grep -c '^# END edge-monitor' "$CADDYFILE_PATH" || true)"
  [ "$begins" = "$ends" ] && [ "$begins" -le 1 ] || die "the markers of an earlier run in $CADDYFILE_PATH are damaged (BEGIN x$begins, END x$ends)" "fix or remove the '# BEGIN edge-monitor' ... '# END edge-monitor' lines by hand, then run again"
  if [ -n "$ACME_EMAIL" ]; then warn "--email is ignored: the existing Caddy keeps its own global options"; ACME_EMAIL=""; fi
fi

PUBLIC_IP="$(meta instance/network-interfaces/0/access-configs/0/external-ip)"
[ -n "$PUBLIC_IP" ] || PUBLIC_IP="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || true)"
if [ -z "$DOMAIN" ]; then
  [ -n "$PUBLIC_IP" ] || die "cannot find this VM's public address, so I cannot pick a host name" "pass one yourself: --domain monitor.example.com (an A record must point at the VM)"
  DOMAIN="${PUBLIC_IP//./-}.sslip.io"
  # a second name for the same address, so it cannot clash with the site the existing Caddy already serves
  [ "$CADDY_MODE" = docker ] || DOMAIN="monitor.$DOMAIN"
  hint "no --domain given: using $DOMAIN (sslip.io turns the address into a name, so HTTPS works with no DNS setup)"
  hint "that name changes if the VM's address changes: reserve a static address (see the end of this run)"
fi
check_inputs
if [ -z "$ADMIN_IPS" ]; then
  warn "no --admin-ip: anyone who finds https://$DOMAIN first can create the admin account, and the admin login is open to the internet"
  hint "restrict it: sudo ./ops/monitor/deploy.sh --admin-ip \$(your home or office address)/32   (your address: https://api.ipify.org)"
fi

case "$CADDY_MODE" in
  docker) proxy_line="run Caddy in Docker (HTTPS for $DOMAIN) next to it" ;;
  host) proxy_line="append a block for $DOMAIN to $CADDYFILE_PATH (backup first, validated, reloaded; undone on any error)" ;;
  none) proxy_line="leave your proxy alone and print the block to add (Kuma listens on 127.0.0.1:3001)" ;;
esac
cat <<EOF

This will:
  - add a 1 GB swap file if the VM has little memory and no swap
  - install Docker (official repository) and automatic security updates
  - run Uptime Kuma ($KUMA_IMAGE) in $APP_DIR
  - $proxy_line
  - set up a nightly backup of the Kuma data in $BACKUP_DIR (14 days kept)
EOF
if [ "$ASSUME_YES" -ne 1 ]; then
  [ -t 0 ] || die "no terminal to ask for confirmation" "add --yes"
  read -r -p "Continue? [y/N] " answer
  case "$answer" in y | Y | yes | YES) ;; *) die "stopped; nothing was changed" ;; esac
fi

say "Memory"
mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
if [ "$mem_kb" -lt 1500000 ] && [ -z "$(swapon --show --noheadings 2>/dev/null)" ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
  ok "created a 1 GB swap file (this VM has $((mem_kb / 1024)) MB of RAM)"
else
  ok "swap not needed"
fi

say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg unattended-upgrades cron >/dev/null
ok "automatic security updates are on (unattended-upgrades)"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "Docker is already installed: $(docker --version)"
else
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID ${VERSION_CODENAME} stable" >/etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null 2>&1 || true
  ok "installed $(docker --version)"
fi

say "Files in $APP_DIR"
install -d -m 755 "$APP_DIR" "$APP_DIR/data" "$BACKUP_DIR"
install -m 644 "$HERE/docker-compose.yml" "$APP_DIR/docker-compose.yml"
profiles=""
[ "$CADDY_MODE" != docker ] || profiles="docker-caddy"
umask 077
cat >"$APP_DIR/.env" <<EOF
DOMAIN=$DOMAIN
ACME_EMAIL=$ACME_EMAIL
ADMIN_IPS=$ADMIN_IPS
KUMA_IMAGE=$KUMA_IMAGE
WATCHDOG_URL=$WATCHDOG_URL
CADDY_MODE=$CADDY_MODE
CADDYFILE_PATH=$CADDYFILE_PATH
COMPOSE_PROFILES=$profiles
EOF
umask 022
if [ "$CADDY_MODE" = docker ]; then
  caddyfile >"$APP_DIR/Caddyfile"
else
  rm -f "$APP_DIR/Caddyfile"
fi
ok "settings saved in $APP_DIR/.env (readable by root only)"

cat >/usr/local/sbin/edge-monitor-backup <<'EOF'
#!/usr/bin/env bash
# Nightly backup of the Uptime Kuma data. Kuma is stopped for a few seconds so the database is consistent.
set -euo pipefail
cd /opt/edge-monitor
mkdir -p /var/backups/edge-monitor
trap 'docker compose start kuma >/dev/null 2>&1 || true' EXIT
docker compose stop kuma >/dev/null
tar czf "/var/backups/edge-monitor/kuma-$(date +%F).tgz" data/kuma
find /var/backups/edge-monitor -name 'kuma-*.tgz' -mtime +14 -delete
EOF
chmod 755 /usr/local/sbin/edge-monitor-backup
echo '17 3 * * * root /usr/local/sbin/edge-monitor-backup' >/etc/cron.d/edge-monitor-backup
chmod 644 /etc/cron.d/edge-monitor-backup
ok "nightly backup at 03:17 -> $BACKUP_DIR (run it by hand: sudo edge-monitor-backup)"

if [ -n "$WATCHDOG_URL" ]; then
  cat >/usr/local/sbin/edge-monitor-watchdog <<'EOF'
#!/usr/bin/env bash
# Pings the external watchdog check, but only while Kuma (and the Caddy in front of it) are up and HTTPS answers.
# If this VM, Docker or the monitor dies, the pings stop and the external service alerts you.
set -euo pipefail
cd /opt/edge-monitor
get() { grep -m1 "^$1=" .env | cut -d= -f2-; }
url="$(get WATCHDOG_URL)"; domain="$(get DOMAIN)"; mode="$(get CADDY_MODE)"
[ -n "$url" ] || exit 0
running="$(docker compose ps --status running --services 2>/dev/null || true)"
printf '%s\n' "$running" | grep -qx kuma || exit 0
[ "$mode" != docker ] || printf '%s\n' "$running" | grep -qx caddy || exit 0
[ "$mode" != host ] || systemctl is-active --quiet caddy || exit 0
case "$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://$domain/" || true)" in
  200 | 301 | 302 | 303 | 403) curl -fsS -m 10 -o /dev/null "$url" ;;
esac
EOF
  chmod 750 /usr/local/sbin/edge-monitor-watchdog
  echo '*/5 * * * * root /usr/local/sbin/edge-monitor-watchdog' >/etc/cron.d/edge-monitor-watchdog
  chmod 644 /etc/cron.d/edge-monitor-watchdog
  ok "watchdog: this VM pings your external check every 5 minutes while the monitor is healthy"
else
  rm -f /etc/cron.d/edge-monitor-watchdog /usr/local/sbin/edge-monitor-watchdog
  warn "no --watchdog-url: if this VM dies you will get no alerts and no warning that you will not"
  hint "make a free check at healthchecks.io (period 5 min, grace 15 min), then run: sudo ./ops/monitor/deploy.sh --watchdog-url https://hc-ping.com/<your-uuid>"
fi

say "Starting the monitor"
cd "$APP_DIR"
docker compose pull -q
# switching away from our own Caddy: remove its container so it stops holding ports 80 and 443
[ "$CADDY_MODE" = docker ] || docker compose --profile docker-caddy rm -sf caddy >/dev/null 2>&1 || true
docker compose up -d --remove-orphans
ok "containers started"
docker compose ps --format 'table {{.Name}}\t{{.Status}}' || true

say "Waiting for Uptime Kuma to start (the first start can take several minutes on a small VM)"
kuma_up=0
for _ in $(seq 1 72); do
  kcode="$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:3001/ 2>/dev/null || true)"
  case "$kcode" in
    200 | 301 | 302 | 303) kuma_up=1; break ;;
  esac
  sleep 5
done
if [ "$kuma_up" -eq 1 ]; then
  ok "Kuma answers on 127.0.0.1:3001"
else
  warn "Kuma did not answer within 6 minutes; visitors get 502 Bad Gateway until it does"
  hint "what Kuma says: sudo docker compose -f $APP_DIR/docker-compose.yml logs --tail 50 kuma"
fi

if [ "$CADDY_MODE" = host ]; then
  apply_caddy_block
fi

if [ "$CADDY_MODE" = none ]; then
  say "Add this to your proxy (Kuma listens on 127.0.0.1:3001)"
  site_block
else
  say "Waiting for HTTPS on https://$DOMAIN (the first certificate takes up to a minute)"
  up=0
  for _ in $(seq 1 36); do
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "https://$DOMAIN/" 2>/dev/null || true)"
    case "$code" in
      200 | 301 | 302 | 303 | 403) up=1; break ;;
    esac
    sleep 5
  done
  if [ "$up" -eq 1 ]; then
    ok "https://$DOMAIN answers (HTTP $code). Let's Encrypt reached this VM, so ports 80 and 443 are open."
    [ "$code" != 403 ] || hint "403 is expected with --admin-ip: this check runs from the VM, whose own address is not allowed. It proves HTTPS works and the restriction is on."
  else
    warn "https://$DOMAIN did not answer within 3 minutes"
    hint "most likely the GCP firewall: ports 80 and 443 must be open to the internet (Let's Encrypt needs port 80). Commands are below."
    hint "if you gave --domain: its A record must point at ${PUBLIC_IP:-the address of this VM}"
    if [ "$CADDY_MODE" = host ]; then
      hint "what Caddy says: sudo journalctl -u caddy -n 50 --no-pager"
    else
      hint "what Caddy says: sudo docker compose -f $APP_DIR/docker-compose.yml logs --tail 50 caddy"
    fi
  fi
fi

VM_NAME="$(meta instance/name)"
VM_ZONE="$(meta instance/zone)"
VM_ZONE="${VM_ZONE##*/}"
VM_REGION="${VM_ZONE%-*}"
if [ "$CADDY_MODE" = host ]; then
  logs_line="sudo journalctl -u caddy -f   (Caddy)   and   sudo docker compose -f $APP_DIR/docker-compose.yml logs -f --tail 100   (Kuma)"
else
  logs_line="sudo docker compose -f $APP_DIR/docker-compose.yml logs -f --tail 100"
fi
cat <<EOF

============================================================
 Deployed. Monitor address: https://$DOMAIN
============================================================

DO THESE NEXT (in this order)
 1. Open https://$DOMAIN (from an address allowed by --admin-ip). Kuma 2 first asks which database to use:
    choose SQLite (light; MariaDB needs far more memory than a small VM has). Then create the admin account
    right now: until you do, the first visitor can.
 2. Settings > Notifications > Setup Notification: pick Telegram or Pushover (no mail server needed), tick
    "Default enabled", and press Test. Alerts must reach your phone, not just this page.
 3. Add New Monitor:  Type = Push,  Friendly Name = "Site heartbeat",
      Heartbeat Interval = 900 seconds (a sender that beats every 5 minutes then tolerates three misses
      before an alert),  Retries = 0.  Save.  Copy the Push URL it shows.
 4. Point the heartbeat sender at that URL, sending about every 5 minutes. For the farm_automation Home Assistant
    package: paste it into the helper "Farm heartbeat URL" (input_text.farm_heartbeat_url).
 5. Test the whole chain: stop the sender (or pull the site's network cable) and confirm the alert reaches
    your phone about 15 minutes later. An alarm you have never seen fire is not an alarm.

IF THE SENDER'S POST IS REJECTED (the monitor stays "pending"; the sender logs 404 or 405 for the URL)
    Your Kuma image only accepts GET on the push URL. Use --image louislam/uptime-kuma:2, or send the heartbeat
    with GET instead of POST.

KEEP IT HEALTHY
 - Keep the address (and so the sslip.io name) from changing: promote the VM's CURRENT address to a reserved one.
   Skip this if the VM already has a static address.
     gcloud compute addresses create edge-monitor-ip --region ${VM_REGION:-REGION} --addresses ${PUBLIC_IP:-CURRENT_ADDRESS}
   (without --addresses it would reserve a different address and the name would change)
 - Open the web ports if they are not open yet (skip if you already did):
     gcloud compute firewall-rules create edge-monitor-web --allow tcp:80,tcp:443 --target-tags edge-monitor
     gcloud compute instances add-tags ${VM_NAME:-VM_NAME} --zone ${VM_ZONE:-ZONE} --tags edge-monitor
 - Watch the watcher: if you did not pass --watchdog-url, do it (see the warning above). Otherwise a dead VM is silent.
 - Update:  cd <your clone> && git pull && sudo ./ops/monitor/deploy.sh
 - Logs:    $logs_line
 - Backups: $BACKUP_DIR (copy them off the VM now and then: gcloud storage cp $BACKUP_DIR/*.tgz gs://YOUR_BUCKET/)
 - Settings changed later (for example a new --admin-ip): run the script again with the new option.
EOF
