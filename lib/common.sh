# shellcheck shell=bash
# Logging and small helpers shared by every part of the deploy.

readonly LOG_FILE="/var/log/edge-server-deploy.log"
readonly STATUS_FILE="/tmp/edge-server-deploy.status"

log() {
    local level="$1"
    shift
    local message="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    printf '%s [%s] %s\n' "$timestamp" "$level" "$message" | stdbuf -oL tee -a "$LOG_FILE"
    # Update status file for progress monitoring
    printf '%s' "$message" 2>/dev/null > "$STATUS_FILE" || true
}

info()    { log "INFO" "$*"; }
success() { log "OK" "$*"; }
warn()    { log "WARN" "$*"; }
error()   { log "ERROR" "$*"; }
fatal()   { error "$*"; exit 1; }

confirm() {
    local prompt="$1"
    local response
    read -rp "$prompt [y/N]: " response
    [[ "$response" =~ ^[Yy]$ ]]
}

# Copy SRC to DST (mode MODE) if it differs; remember that something changed.
install_if_changed() {
    local src="$1" dst="$2" mode="$3"
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        chmod "$mode" "$dst"
        return 0
    fi
    install -m "$mode" "$src" "$dst"
    TELEGRAM_CHANGED=true
}

# Where a plugin lives: next to this script, in plugins/ or directly.
find_plugin_dir() {
    local name="$1" script_dir
    script_dir="$EDGE_SCRIPT_DIR"
    if [[ -d "${script_dir}/plugins/${name}" ]]; then
        echo "${script_dir}/plugins/${name}"
    elif [[ -d "${script_dir}/${name}" ]]; then
        echo "${script_dir}/${name}"
    else
        return 1
    fi
}
