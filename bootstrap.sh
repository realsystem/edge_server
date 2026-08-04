#!/usr/bin/env bash
# Bootstrap script - runs on laptop, deploys to remote target over SSH
# Usage: ./bootstrap.sh [options] <target-ip>

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

# Defaults
MODE="manual"
DEPLOY_TYPE="full"
SSH_USER="${USER}"
SECRETS_FILE=""
SKIP_INIT=false
DRY_RUN=false
TARGET=""
REMOTE_DIR="/tmp/edge-server-setup"

# Script directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

#-----------------------------------------------------------------------------
# Helpers
#-----------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: $(basename "$0") [options] <target-ip>

Bootstrap edge server deployment from your laptop to a remote Ubuntu server.

Options:
  --auto              Non-interactive mode (requires --secrets-file or env vars)
  --secrets-file FILE Path to secrets file (.env format)
  --deploy TYPE       Deployment type: base, security, full (default: full)
  --user USER         SSH user (default: current user)
  --skip-init         Skip initial setup (target already configured)
  --dry-run           Show what would happen without executing
  -h, --help          Show this help

Examples:
  # Interactive manual mode
  ./bootstrap.sh 192.168.1.100

  # Automated with secrets file
  ./bootstrap.sh --auto --secrets-file ~/.edge-secrets.env 192.168.1.100

  # Security stack only, skip initial setup
  ./bootstrap.sh --deploy security --skip-init 192.168.1.100

  # Dry run to see what would happen
  ./bootstrap.sh --dry-run 192.168.1.100

Secrets file format (.edge-secrets.env):
  TAILSCALE_AUTH_KEY=tskey-auth-xxxxx
  MQTT_USER=homeassistant
  MQTT_PASS=secretpassword
  REOLINK_USER=admin
  REOLINK_PASS=camerapassword
  EXTERNAL_DRIVE_UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
EOF
    exit 0
}

log() { echo -e "  $1"; }
info() { echo -e "  ${BLUE}→${NC} $1"; }
ok() { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
phase() {
    echo ""
    echo -e "${BOLD}Phase $1: $2${NC}"
}

dry() {
    if [ "$DRY_RUN" = true ]; then
        echo -e "  ${YELLOW}[DRY-RUN]${NC} $1"
        return 0
    fi
    return 1
}

confirm() {
    if [ "$MODE" = "auto" ]; then
        return 0
    fi
    local prompt="${1:-Continue?}"
    read -r -p "  $prompt [Y/n] " response
    case "$response" in
        [nN][oO]|[nN]) return 1 ;;
        *) return 0 ;;
    esac
}

prompt_value() {
    local var_name="$1"
    local prompt="$2"
    local default="${3:-}"
    local secret="${4:-false}"

    if [ "$MODE" = "auto" ]; then
        # In auto mode, value must be set
        local val="${!var_name:-}"
        if [ -z "$val" ] && [ -z "$default" ]; then
            fail "Required value $var_name not set (auto mode)"
            exit 1
        fi
        echo "${val:-$default}"
        return
    fi

    local display_default=""
    [ -n "$default" ] && display_default=" [$default]"

    if [ "$secret" = true ]; then
        read -r -s -p "  $prompt$display_default: " value
        echo ""
    else
        read -r -p "  $prompt$display_default: " value
    fi

    echo "${value:-$default}"
}

ssh_cmd() {
    ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "${SSH_USER}@${TARGET}" "$@"
}

scp_cmd() {
    scp -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$@"
}

wait_for_host() {
    local max_wait="${1:-120}"
    local waited=0
    info "Waiting for target to come back online..."
    while [ "$waited" -lt "$max_wait" ]; do
        if ssh_cmd "true" 2>/dev/null; then
            ok "Target back online (took ${waited}s)"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    fail "Target did not come back online within ${max_wait}s"
    return 1
}

#-----------------------------------------------------------------------------
# Parse arguments
#-----------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case $1 in
        --auto)
            MODE="auto"
            shift
            ;;
        --secrets-file)
            SECRETS_FILE="$2"
            shift 2
            ;;
        --deploy)
            DEPLOY_TYPE="$2"
            if [[ ! "$DEPLOY_TYPE" =~ ^(base|security|full)$ ]]; then
                echo "Error: --deploy must be base, security, or full"
                exit 1
            fi
            shift 2
            ;;
        --user)
            SSH_USER="$2"
            shift 2
            ;;
        --skip-init)
            SKIP_INIT=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        -*)
            echo "Unknown option: $1"
            exit 1
            ;;
        *)
            TARGET="$1"
            shift
            ;;
    esac
done

if [ -z "$TARGET" ]; then
    echo "Error: Target IP required"
    echo "Usage: $(basename "$0") [options] <target-ip>"
    exit 1
fi

# Load secrets file if provided
if [ -n "$SECRETS_FILE" ]; then
    if [ -f "$SECRETS_FILE" ]; then
        # shellcheck disable=SC1090
        source "$SECRETS_FILE"
    else
        echo "Error: Secrets file not found: $SECRETS_FILE"
        exit 1
    fi
fi

#-----------------------------------------------------------------------------
# Header
#-----------------------------------------------------------------------------

MODE_DISPLAY=$(echo "$MODE" | awk '{print toupper(substr($0,1,1)) tolower(substr($0,2))}')
echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo -e " ${BOLD}Edge Server Bootstrap${NC} - ${MODE_DISPLAY} Mode"
[ "$DRY_RUN" = true ] && echo -e " ${YELLOW}(DRY RUN - no changes will be made)${NC}"
echo "═══════════════════════════════════════════════════════════════════"

#-----------------------------------------------------------------------------
# Phase 1: Discovery
#-----------------------------------------------------------------------------

phase 1 "Discovery"

info "Target: $TARGET"
info "SSH user: $SSH_USER"
info "Deploy type: $DEPLOY_TYPE"

if ! dry "Would test SSH connection"; then
    if ssh_cmd "true" 2>/dev/null; then
        ok "SSH connection OK"
    else
        fail "Cannot connect via SSH to ${SSH_USER}@${TARGET}"
        if [ "$MODE" = "manual" ]; then
            echo ""
            echo "  Troubleshooting:"
            echo "    1. Verify target IP is correct"
            echo "    2. Ensure SSH is enabled on target"
            echo "    3. Check SSH key is authorized: ssh-copy-id ${SSH_USER}@${TARGET}"
        fi
        exit 1
    fi

    # Check OS
    os_info=$(ssh_cmd "cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'" || echo "")
    if [[ "$os_info" == *"Ubuntu"* ]]; then
        ok "OS: $os_info"
    else
        warn "OS: ${os_info:-Unknown} (expected Ubuntu)"
        if [ "$MODE" = "manual" ]; then
            confirm "Continue anyway?" || exit 1
        fi
    fi

    # Check for existing deployment
    if ssh_cmd "[ -d /opt/edge-server ]" 2>/dev/null; then
        warn "Existing deployment detected at /opt/edge-server"
        if [ "$MODE" = "manual" ]; then
            confirm "Continue with upgrade?" || exit 1
        fi
    fi
fi

#-----------------------------------------------------------------------------
# Phase 2: Prerequisites
#-----------------------------------------------------------------------------

phase 2 "Prerequisites"

# Check local tools
for tool in ssh scp; do
    if command -v $tool >/dev/null 2>&1; then
        ok "Local tool: $tool"
    else
        fail "Missing local tool: $tool"
        exit 1
    fi
done

# Copy scripts to target
if ! dry "Would copy scripts to $TARGET:$REMOTE_DIR"; then
    info "Copying scripts to target..."
    ssh_cmd "mkdir -p $REMOTE_DIR"
    scp_cmd -r \
        "$SCRIPT_DIR/initial-setup.sh" \
        "$SCRIPT_DIR/deploy-edge-server.sh" \
        "$SCRIPT_DIR/deploy-security.sh" \
        "$SCRIPT_DIR/secrets.sh" \
        "$SCRIPT_DIR/env.example" \
        "${SSH_USER}@${TARGET}:${REMOTE_DIR}/"
    ssh_cmd "chmod +x $REMOTE_DIR/*.sh"
    ok "Scripts copied to target"
fi

#-----------------------------------------------------------------------------
# Phase 3: Initial Setup
#-----------------------------------------------------------------------------

if [ "$SKIP_INIT" = true ]; then
    phase 3 "Initial Setup (skipped)"
    ok "Skipping initial setup as requested"
else
    phase 3 "Initial Setup"

    # Gather configuration
    if [ "$MODE" = "manual" ]; then
        echo ""
        log "Configure static IP (leave blank to skip):"
    fi

    STATIC_IP=$(prompt_value "STATIC_IP" "Static IP (e.g., 192.168.1.100/24)" "")
    GATEWAY=$(prompt_value "GATEWAY" "Gateway" "")
    DNS=$(prompt_value "DNS" "DNS server" "8.8.8.8")
    DRIVE_UUID=$(prompt_value "EXTERNAL_DRIVE_UUID" "External drive UUID (optional)" "")

    if ! dry "Would run initial-setup.sh on target"; then
        info "Running initial setup on target..."

        # Build environment for initial setup
        init_env=""
        [ -n "$STATIC_IP" ] && init_env+="STATIC_IP='$STATIC_IP' "
        [ -n "$GATEWAY" ] && init_env+="GATEWAY='$GATEWAY' "
        [ -n "$DNS" ] && init_env+="DNS='$DNS' "
        [ -n "$DRIVE_UUID" ] && init_env+="EXTERNAL_DRIVE_UUID='$DRIVE_UUID' "

        if ssh_cmd "cd $REMOTE_DIR && sudo ${init_env} ./initial-setup.sh"; then
            ok "Initial setup complete"
        else
            fail "Initial setup failed"
            exit 1
        fi

        # Reboot
        if [ "$MODE" = "manual" ]; then
            if confirm "Reboot target now?"; then
                info "Rebooting target..."
                ssh_cmd "sudo reboot" || true
                sleep 5
                wait_for_host 120
            fi
        else
            info "Rebooting target..."
            ssh_cmd "sudo reboot" || true
            sleep 5
            wait_for_host 120
        fi
    fi
fi

#-----------------------------------------------------------------------------
# Phase 4: Secrets Configuration
#-----------------------------------------------------------------------------

phase 4 "Secrets Configuration"

if [ "$MODE" = "manual" ]; then
    echo ""
    log "Enter credentials (leave blank to skip):"
fi

TS_KEY=$(prompt_value "TAILSCALE_AUTH_KEY" "Tailscale auth key" "" true)
MQTT_USER=$(prompt_value "MQTT_USER" "MQTT username" "homeassistant")
MQTT_PASS=$(prompt_value "MQTT_PASS" "MQTT password" "" true)
REOLINK_USER=$(prompt_value "REOLINK_USER" "Camera username" "admin")
REOLINK_PASS=$(prompt_value "REOLINK_PASS" "Camera password" "" true)

if ! dry "Would configure secrets on target"; then
    info "Configuring secrets on target..."

    # Initialize secrets on target
    ssh_cmd "cd $REMOTE_DIR && ./secrets.sh init 2>/dev/null || true"

    # Set each secret
    [ -n "$TS_KEY" ] && ssh_cmd "cd $REMOTE_DIR && ./secrets.sh set TAILSCALE_AUTH_KEY '$TS_KEY'"
    [ -n "$MQTT_USER" ] && ssh_cmd "cd $REMOTE_DIR && ./secrets.sh set MQTT_USER '$MQTT_USER'"
    [ -n "$MQTT_PASS" ] && ssh_cmd "cd $REMOTE_DIR && ./secrets.sh set MQTT_PASS '$MQTT_PASS'"
    [ -n "$REOLINK_USER" ] && ssh_cmd "cd $REMOTE_DIR && ./secrets.sh set REOLINK_USER '$REOLINK_USER'"
    [ -n "$REOLINK_PASS" ] && ssh_cmd "cd $REMOTE_DIR && ./secrets.sh set REOLINK_PASS '$REOLINK_PASS'"

    ok "Secrets configured"
fi

#-----------------------------------------------------------------------------
# Phase 5: Deployment
#-----------------------------------------------------------------------------

phase 5 "Deployment"

deploy_base() {
    if ! dry "Would run deploy-edge-server.sh"; then
        info "Running deploy-edge-server.sh..."
        if ssh_cmd "cd $REMOTE_DIR && eval \$(./secrets.sh export) && sudo -E ./deploy-edge-server.sh"; then
            ok "Base stack deployed"
        else
            fail "Base stack deployment failed"
            return 1
        fi
    fi
}

deploy_security() {
    if ! dry "Would run deploy-security.sh"; then
        info "Running deploy-security.sh..."
        if ssh_cmd "cd $REMOTE_DIR && eval \$(./secrets.sh export) && sudo -E ./deploy-security.sh"; then
            ok "Security stack deployed"
        else
            fail "Security stack deployment failed"
            return 1
        fi
    fi
}

case "$DEPLOY_TYPE" in
    base)
        deploy_base
        ;;
    security)
        deploy_security
        ;;
    full)
        deploy_base && deploy_security
        ;;
esac

#-----------------------------------------------------------------------------
# Phase 6: Verification
#-----------------------------------------------------------------------------

phase 6 "Verification"

if [ "$DRY_RUN" = true ]; then
    dry "Would verify services are running"
else
    info "Waiting 15s for services to initialize..."
    sleep 15

    # Check Tailscale
    ts_status=$(ssh_cmd "tailscale status --json 2>/dev/null | grep -o '\"BackendState\":\"[^\"]*\"' | cut -d'\"' -f4" || echo "")
    if [ "$ts_status" = "Running" ]; then
        ts_hostname=$(ssh_cmd "tailscale status --json 2>/dev/null | grep -o '\"Self\":{[^}]*' | grep -o '\"DNSName\":\"[^\"]*\"' | cut -d'\"' -f4 | sed 's/\\.$//' " || echo "unknown")
        ok "Tailscale: connected ($ts_hostname)"
    else
        warn "Tailscale: ${ts_status:-not running}"
    fi

    # Check Home Assistant
    ha_status=$(ssh_cmd "curl -s -o /dev/null -w '%{http_code}' http://localhost:8123/api/ 2>/dev/null" || echo "000")
    if [ "$ha_status" = "200" ] || [ "$ha_status" = "401" ]; then
        ok "Home Assistant: http://${TARGET}:8123"
    else
        warn "Home Assistant: not responding (HTTP $ha_status)"
    fi

    # Check MQTT
    if ssh_cmd "docker exec mosquitto mosquitto_sub -t '\$SYS/#' -C 1 -W 3 >/dev/null 2>&1"; then
        ok "MQTT: ${TARGET}:1883"
    else
        warn "MQTT: not responding"
    fi

    # Check Frigate (if security deployed)
    if [ "$DEPLOY_TYPE" = "security" ] || [ "$DEPLOY_TYPE" = "full" ]; then
        frigate_status=$(ssh_cmd "curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/api/version 2>/dev/null" || echo "000")
        if [ "$frigate_status" = "200" ]; then
            ok "Frigate: http://${TARGET}:5000"
        else
            warn "Frigate: not responding (HTTP $frigate_status)"
        fi
    fi
fi

#-----------------------------------------------------------------------------
# Summary
#-----------------------------------------------------------------------------

echo ""
echo "═══════════════════════════════════════════════════════════════════"
if [ "$DRY_RUN" = true ]; then
    echo -e " ${YELLOW}Dry run complete - no changes were made${NC}"
else
    echo -e " ${GREEN}${BOLD}Deployment complete!${NC}"
    echo ""
    echo " Access your server:"
    echo "   Home Assistant: http://${TARGET}:8123"
    [ "$DEPLOY_TYPE" != "base" ] && echo "   Frigate:        http://${TARGET}:5000"
    echo "   MQTT:           ${TARGET}:1883"
    echo "   SSH:            ssh ${SSH_USER}@${TARGET}"
fi
echo "═══════════════════════════════════════════════════════════════════"
echo ""
