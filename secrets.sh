#!/usr/bin/env bash
#===============================================================================
# Edge Server Secrets Management
# Lightweight local secrets storage using encrypted file or pass
#
# Usage:
#   ./secrets.sh init              - Initialize secrets storage
#   ./secrets.sh set KEY VALUE     - Store a secret
#   ./secrets.sh set KEY -         - Store a secret read from stdin (keeps it out of `ps`)
#   ./secrets.sh get KEY           - Retrieve a secret
#   ./secrets.sh list              - List all secret keys
#   ./secrets.sh export            - Export secrets as env vars (source this)
#   ./secrets.sh edit              - Edit secrets file directly
#===============================================================================

set -euo pipefail

# Every file this script creates (incl. the temporary plaintext) is owner-only.
umask 077

SECRETS_DIR="${SECRETS_DIR:-$HOME/.edge-server-secrets}"
SECRETS_FILE="$SECRETS_DIR/secrets.enc"
SECRETS_PLAIN="$SECRETS_DIR/.secrets.tmp"

# Never leave decrypted secrets behind, even when a command fails midway.
cleanup() { rm -f "$SECRETS_PLAIN" "${SECRETS_PLAIN}.new"; }
trap cleanup EXIT

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
        printf '%s\n' "$SECRETS_PASSWORD"
        return
    fi
    local pass=""
    IFS= read -rsp "Secrets password: " pass || true
    echo >&2
    printf '%s\n' "$pass"
}

# Run openssl with the password passed through the environment rather than
# argv, so it does not show up in `ps` output for other users.
run_openssl() {
    local pass="$1"
    shift
    EDGE_SECRETS_PASS="$pass" openssl enc -aes-256-cbc -pbkdf2 "$@" -pass env:EDGE_SECRETS_PASS
}

validate_key() {
    if [[ ! "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        error "Invalid key '$1' (use letters, digits and underscores)"
        exit 1
    fi
}

init_secrets() {
    if [[ -f "$SECRETS_FILE" ]]; then
        warn "Secrets file already exists: $SECRETS_FILE"
        read -rp "Reinitialize? This will DELETE existing secrets [y/N]: " confirm
        [[ ! "$confirm" =~ ^[Yy]$ ]] && exit 0
    fi

    mkdir -p "$SECRETS_DIR"
    chmod 700 "$SECRETS_DIR"

    local pass1="" pass2=""
    IFS= read -rsp "Create secrets password: " pass1 || true
    echo
    IFS= read -rsp "Confirm password: " pass2 || true
    echo

    if [[ -z "$pass1" ]]; then
        error "Password cannot be empty"
        exit 1
    fi

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
    run_openssl "$pass1" -salt -in "$SECRETS_PLAIN" -out "$SECRETS_FILE"
    rm -f "$SECRETS_PLAIN"
    chmod 600 "$SECRETS_FILE"

    info "Secrets initialized at $SECRETS_FILE"
    echo "Run: ./secrets.sh edit   to add your secrets"
}

decrypt_secrets() {
    local pass="${1:-}"
    [[ -n "$pass" ]] || pass=$(get_password)
    # Decrypt to a private temp file first: with a wrong password openssl may
    # emit garbage before failing, which must never reach stdout.
    local out
    out=$(mktemp "$SECRETS_DIR/.decrypt.XXXXXX")
    if ! run_openssl "$pass" -d -in "$SECRETS_FILE" -out "$out" 2>/dev/null; then
        rm -f "$out"
        error "Failed to decrypt secrets (wrong password?)"
        exit 1
    fi
    cat "$out"
    rm -f "$out"
}

encrypt_secrets() {
    local pass="$1"
    run_openssl "$pass" -salt -in "$SECRETS_PLAIN" -out "$SECRETS_FILE"
    rm -f "$SECRETS_PLAIN"
}

set_secret() {
    local key="$1"
    local value="$2"
    local pass

    validate_key "$key"

    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    pass=$(get_password)

    # "-" reads the value from stdin (after the password, if that is piped too)
    if [[ "$value" == "-" ]]; then
        value=""
        IFS= read -r value || true
    fi
    if [[ "$value" == *$'\n'* ]]; then
        error "Secret values cannot contain newlines"
        exit 1
    fi

    decrypt_secrets "$pass" > "$SECRETS_PLAIN"

    # Replace the key (or append it). Plain string handling instead of sed, so
    # values containing |, &, / or \ are stored verbatim.
    local replaced=false line
    : > "${SECRETS_PLAIN}.new"
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "${key}="* ]]; then
            if [[ "$replaced" == false ]]; then
                printf '%s=%s\n' "$key" "$value" >> "${SECRETS_PLAIN}.new"
                replaced=true
            fi
        else
            printf '%s\n' "$line" >> "${SECRETS_PLAIN}.new"
        fi
    done < "$SECRETS_PLAIN"
    if [[ "$replaced" == false ]]; then
        printf '%s=%s\n' "$key" "$value" >> "${SECRETS_PLAIN}.new"
    fi
    mv "${SECRETS_PLAIN}.new" "$SECRETS_PLAIN"

    encrypt_secrets "$pass"
    info "Secret '$key' updated"
}

get_secret() {
    local key="$1"
    validate_key "$key"

    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    local plain
    plain=$(decrypt_secrets)
    # A missing key prints nothing (exit 0); a wrong password exits non-zero.
    grep "^${key}=" <<< "$plain" | head -n1 | cut -d'=' -f2- || true
}

list_secrets() {
    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    local plain
    plain=$(decrypt_secrets)
    echo "Stored secrets:"
    grep -v '^#' <<< "$plain" | grep -v '^$' | cut -d'=' -f1 | while read -r key; do
        echo "  - $key"
    done || true
}

export_secrets() {
    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    # Output format suitable for: eval "$(./secrets.sh export)"
    # Values are shell-quoted with %q so quotes, $, ` and spaces in a secret
    # cannot break out of the assignment when the output is eval'd.
    local plain key value
    plain=$(decrypt_secrets)
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        printf 'export %s=%q\n' "$key" "$value"
    done <<< "$plain"
}

edit_secrets() {
    local pass

    if [[ ! -f "$SECRETS_FILE" ]]; then
        error "Secrets not initialized. Run: ./secrets.sh init"
        exit 1
    fi

    pass=$(get_password)
    decrypt_secrets "$pass" > "$SECRETS_PLAIN"

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
    echo "  set KEY VALUE     Store a secret (use '-' as VALUE to read it from stdin)"
    echo "  get KEY           Retrieve a secret"
    echo "  list              List all secret keys"
    echo "  export            Export as env vars (use with eval)"
    echo "  edit              Edit secrets file"
    echo ""
    echo "Examples:"
    echo "  $0 init"
    echo "  $0 set MQTT_PASS 'my-secure-password'"
    echo "  $0 get MQTT_PASS"
    echo "  eval \"\$($0 export)\"   # Load secrets into environment"
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
