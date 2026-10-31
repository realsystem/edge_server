#!/usr/bin/env bash
#===============================================================================
# Start the Telegram bot exactly as the deploy script configures it (generated
# compose file: read-only root, no capabilities, pid: host) and check that the
# diagnostics it runs for /diag work inside that container and leak no password.
#
# Needs Docker. The bot cannot reach Telegram here (fake token): it only has to
# start and run the report. Telegram behaviour is covered by tests/test_telegram_bot.py.
#
# Usage: tests/validate-telegram-bot.sh
#   SUDO=sudo  make the planted config root-owned, as on a real server (CI sets
#              this; the container has no CAP_DAC_OVERRIDE, so file owner matters)
#===============================================================================

set -Eeuo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
SUDO="${SUDO:-}"

WORK="$(mktemp -d)"
APP="$WORK/app"
cleanup() {
    (cd "$APP" 2> /dev/null && $SUDO docker compose down -v --timeout 5 > /dev/null 2>&1) || true
    $SUDO rm -rf "$WORK"
}
trap cleanup EXIT

# In GitHub Actions the log is not readable through the API, so put the reason
# (and the bot's own log) into an error annotation.
annotate() {
    [[ -n "${GITHUB_ACTIONS:-}" ]] || return 0
    local extra msg
    extra="$(
        {
            tail -n 12 "$WORK/gen.log" 2> /dev/null
            docker logs --tail 12 telegram-bot 2>&1
        } | sed ':a;N;$!ba;s/%/%25/g;s/\r//g;s/\n/%0A/g'
    )"
    msg="$1%0A${extra}"
    echo "::error title=Telegram bot container check failed::${msg}"
}
fail() {
    echo "FAIL: $*" >&2
    annotate "$*"
    trap - ERR
    exit 1
}
trap 'annotate "line $LINENO: $BASH_COMMAND"' ERR

# A made-up but well-formed token and a planted camera password
TOKEN="123456789:AAH$(printf 'x%.0s' {1..30})"
PASSWORD="s3cr3t-cam-pass"
TOTP_SECRET="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ALLOWED_IDS=1001\nREBOOT_TOTP_SECRET=%s\n' \
    "$TOKEN" "$TOTP_SECRET" > "$WORK/.telegram"
mkdir -p "$APP/frigate"

echo "== generate the stack with the deploy script (builds the image)"
env EDGE_SERVER_DIR="$APP" EDGE_STORAGE_DIR="$WORK/storage" \
    TELEGRAM_CREDENTIALS_FILE="$WORK/.telegram" \
    bash -c '
        source "$1"
        log() { :; }; info() { :; }; warn() { echo "warn: $*" >&2; }; step() { :; }
        success() { :; }; error() { echo "$*" >&2; }
        chown() { :; }
        prepare_telegram_bot
        [[ "$INSTALL_TELEGRAM_BOT" == "true" ]] || { echo "bot was not enabled" >&2; exit 1; }
        [[ "$TELEGRAM_REBOOT_ENABLED" == "true" ]] || { echo "/reboot was not enabled" >&2; exit 1; }
        create_app_stack
    ' _ "$REPO/deploy-edge-server.sh" > "$WORK/gen.log" 2>&1
mkdir -p "$WORK/storage/frigate"

# What a real server has: root-owned, private config and camera logins
cat > "$APP/frigate/config.yml" << CFG
cameras:
  front:
    ffmpeg:
      inputs:
        - path: "rtsp://admin:${PASSWORD}@192.168.1.20:554/main"
          roles: [detect, record]
CFG
echo "192.168.1.20 admin:${PASSWORD}" > "$APP/.cam"
chmod 600 "$APP/frigate/config.yml" "$APP/.cam"
$SUDO chown -R root:root "$APP"

echo "== compose file is valid"
(cd "$APP" && $SUDO docker compose config -q)  # reads the root-only .telegram

echo "== start only the bot"
(cd "$APP" && $SUDO docker compose up -d --no-deps telegram-bot)
sleep 5
state=$(docker inspect -f '{{.State.Status}}' telegram-bot)
[[ "$state" == "running" ]] || { docker logs telegram-bot || true; fail "bot container is $state"; }
docker logs telegram-bot 2>&1 | grep -q "bot started" || { docker logs telegram-bot; fail "bot did not start"; }
docker logs telegram-bot 2>&1 | grep -q "/reboot" || fail "bot did not enable /reboot"

echo "== the container has what /diag needs"
docker exec telegram-bot python3 /app/bot.py --check | tee /dev/stderr | grep -q "check ok" ||
    fail "bot.py --check"

echo "== run the diagnostics inside the hardened container"
docker exec -e SAMPLES=1 -e INTERVAL=1 -e FRIGATE_URL=http://127.0.0.1:9 telegram-bot \
    bash /app/collect-diagnostics.sh /tmp/report.txt 2> /dev/null
report="$WORK/report.txt"
docker exec telegram-bot cat /tmp/report.txt > "$report"

for section in "===== System" "===== CPU" "===== GPU and accelerators" "Busiest processes" "===== Frigate config"; do
    grep -qF "$section" "$report" || fail "report has no '$section' section"
done
grep -q "cameras:" "$report" || fail "report does not include the Frigate config (unreadable?)"
grep -q "@192.168.1.20:554/main" "$report" || fail "camera host missing from the redacted config"
if grep -q "$PASSWORD" "$report"; then fail "the camera password is in the report"; fi
if grep -q "$TOKEN" "$report"; then fail "the bot token is in the report"; fi
if grep -q "$TOTP_SECRET" "$report"; then fail "the reboot secret is in the report"; fi
# pid: host - the process list shows the host, not just the container
grep -qE 'dockerd|containerd|systemd|/sbin/init' "$report" || fail "host processes not visible (pid: host)"

echo "== hardening is in effect"
[[ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' telegram-bot)" == "true" ]] || fail "root fs is writable"
[[ "$(docker inspect -f '{{.HostConfig.CapDrop}}' telegram-bot)" == "[ALL]" ]] || fail "capabilities not dropped"
[[ -z "$(docker inspect -f '{{.HostConfig.PortBindings}}' telegram-bot | tr -d 'map[]')" ]] || fail "bot publishes ports"
if docker exec telegram-bot sh -c 'touch /opt/edge-server/x 2>/dev/null'; then fail "app dir is writable"; fi

echo "== the drop box is the one writable path"
docker exec telegram-bot sh -c 'touch /requests/probe && rm /requests/probe' ||
    fail "the bot cannot write to /requests (needed for /reboot)"
if docker exec telegram-bot sh -c 'touch /app/x 2>/dev/null'; then fail "bot code dir is writable"; fi

echo "Telegram bot container OK"
