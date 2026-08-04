#!/usr/bin/env bash
#===============================================================================
# Edge Server Secrets Management
# Lightweight local secrets storage using encrypted file or pass
#
# Usage:
#   ./secrets.sh init              - Initialize secrets storage
#   ./secrets.sh set KEY VALUE     - Store a secret
#   ./secrets.sh get KEY           - Retrieve a secret
#   ./secrets.sh list              - List all secret keys
#   ./secrets.sh export            - Export secrets as env vars (source this)
#   ./secrets.sh edit              - Edit secrets file directly
#===============================================================================

set -euo pipefail

SECRETS_DIR="${SECRETS_DIR:-$HOME/.edge-server-secrets}"
SECRETS_FILE="$SECRETS_DIR/secrets.enc"
SECRETS_PLAIN="$SECRETS_DIR/.secrets.tmp"

info()  { echo "[OK] $*"; }
error() { echo "[ERROR] $*" >&2; }
warn()  { echo "[WARN] $*"; }

check_deps() {
    if ! command -v openssl &>/dev/null; then
        error "openssl required but not found"
        exit 1
    fi
}

get_password() {
    if [[ -n "${SECRETS_PASSWORD:-}" ]]; then
        echo "$SECRETS_PASSWORD"
        return
    fi
    read -rsp "Secrets password: " pass
    echo >&2
    echo "$pass"
}

init_secrets() {
    if [[ -f "$SECRETS_FILE" ]]; then
        warn "Secrets file already exists: $SECRETS_FILE"
        read -rp "Reinitialize? This will DELETE existing secrets [y/N]: " confirm
        [[ ! "$confirm" =~ ^[Yy]$ ]] && exit 0
    fi

    mkdir -p "$SECRETS_DIR"
    chmod 700 "$SECRETS_DIR"

    read -rsp "Create secrets password: " pass1
    echo
    read -rsp "Confirm password: " pass2
    echo

    if [[ "$pass1" != "$pass2" ]]; then
        error "Passwords do not match"
        exit 1
    fi

    # Create empty secrets file with template
    cat > "$SECRETS_PLAIN" << 'EOF'
# Edge Server Secrets
# Format: KEY=value (one per line)
# Lines starting with # are comments

# Tailscale auth key (from https://login.tailscale.com/admin/settings/keys)
TAILSCALE_AUTH_KEY=

# MQTT credentials
MQTT_USER=admin
MQTT_PASS=

# Camera credentials (Reolink)
REOLINK_USER=admin
REOLINK_PASS=

# External drive UUID (from: lsblk -o NAME,UUID)
EXTERNAL_DRIVE_UUID=
EOF

    # Encrypt
    openssl enc -aes-256-cbc -salt -pbkdf2 -in "$SECRETS_PLAIN" -out "$SECRETS_FILE" -pass "pass:$pass1"
    rm -f "$SECRETS_PLAIN"
    chmod 600 "$SECRETS_FILE"

    info "Secrets initialized at $SECRETS_FILE"
    echo "Run: ./secrets.sh edit   to add your secrets"
}

decrypt_secrets() {
    local pass
    pass=$(get_password)
    openssl enc -aes-256-cbc -d -pbkdf2 -in "$SECRETS_FILE" -pass "pass:$pass" 2>/dev/null || {
        error "Failed to decrypt secrets (wrong password?)"
        exit 1
    }
}

encrypt_secrets() {
    local pass="$1"
    openssl enc -aes-256-cbc -salt -pbkdf2 -in "$SECRETS_PLAIN" -out "$SECRETS_FILE" -pass "pass:$pass"
    rm -f "$SECRETS_PLAIN"
}

set_secret() {
    local key="$1"
    local value="$2"
    local pass

    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    pass=$(get_password)
    decrypt_secrets > "$SECRETS_PLAIN"

    # Update or add key
    if grep -q "^${key}=" "$SECRETS_PLAIN"; then
        sed -i.bak "s|^${key}=.*|${key}=${value}|" "$SECRETS_PLAIN"
        rm -f "${SECRETS_PLAIN}.bak"
    else
        echo "${key}=${value}" >> "$SECRETS_PLAIN"
    fi

    encrypt_secrets "$pass"
    info "Secret '$key' updated"
}

get_secret() {
    local key="$1"

    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    decrypt_secrets | grep "^${key}=" | cut -d'=' -f2- || true
}

list_secrets() {
    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    echo "Stored secrets:"
    decrypt_secrets | grep -v '^#' | grep -v '^$' | cut -d'=' -f1 | while read -r key; do
        echo "  - $key"
    done
}

export_secrets() {
    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    # Output format suitable for: eval $(./secrets.sh export)
    decrypt_secrets | grep -v '^#' | grep -v '^$' | while IFS='=' read -r key value; do
        [[ -n "$key" ]] && echo "export ${key}=\"${value}\""
    done
}

edit_secrets() {
    local pass

    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    pass=$(get_password)
    decrypt_secrets > "$SECRETS_PLAIN"
    chmod 600 "$SECRETS_PLAIN"

    # Use editor
    "${EDITOR:-vi}" "$SECRETS_PLAIN"

    encrypt_secrets "$pass"
    info "Secrets updated"
}

usage() {
    echo "Edge Server Secrets Management"
    echo ""
    echo "Usage: $0 <command> [args]"
    echo ""
    echo "Commands:"
    echo "  init              Initialize secrets storage"
    echo "  set KEY VALUE     Store a secret"
    echo "  get KEY           Retrieve a secret"
    echo "  list              List all secret keys"
    echo "  export            Export as env vars (use with eval)"
    echo "  edit              Edit secrets file"
    echo ""
    echo "Examples:"
    echo "  $0 init"
    echo "  $0 set MQTT_PASS 'my-secure-password'"
    echo "  $0 get MQTT_PASS"
    echo "  eval \$($0 export)   # Load secrets into environment"
    echo ""
    echo "Environment:"
    echo "  SECRETS_DIR       Override secrets directory (default: ~/.edge-server-secrets)"
    echo "  SECRETS_PASSWORD  Provide password non-interactively (for scripts)"
}

main() {
    check_deps

    local cmd="${1:-}"
    shift || true

    case "$cmd" in
        init)
            init_secrets
            ;;
        set)
            [[ $# -lt 2 ]] && { error "Usage: $0 set KEY VALUE"; exit 1; }
            set_secret "$1" "$2"
            ;;
        get)
            [[ $# -lt 1 ]] && { error "Usage: $0 get KEY"; exit 1; }
            get_secret "$1"
            ;;
        list)
            list_secrets
            ;;
        export)
            export_secrets
            ;;
        edit)
            edit_secrets
            ;;
        -h|--help|help|"")
            usage
            ;;
        *)
            error "Unknown command: $cmd"
            usage
            exit 1
            ;;
    esac
}

main "$@"
