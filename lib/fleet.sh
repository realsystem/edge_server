# shellcheck shell=bash
# Fleet mode: --config fleet.yaml --node NAME (see docs/design/edge-system.md).
# A setting comes from the first of: command-line flags, environment variables, the
# node's entry in the fleet file, deploy.env of the last run (--auto), built-in defaults.
# Without a fleet file none of this runs and the deploy behaves as it always has.

FLEET_FILE="" FLEET_NODE="" FLEET_ROLE="" FLEET_WRITE=""
FLEET_CLASSIC=false FLEET_CHECK=false FLEET_ACTIVE=false FLEET_FROM_SAVED=false
_fleet_dir="${EDGE_SERVER_DIR:-/opt/edge-server}"
FLEET_SAVED_FILE="$_fleet_dir/fleet.yaml"    # the last file that deployed successfully
FLEET_SAVED_STATE="$_fleet_dir/fleet.state"  # node=NAME and, if overridden, roles=LIST
FLEET_TOOL="$EDGE_SCRIPT_DIR/tools/fleet.py"

fleet_state_value() { sed -n "s/^$1=//p" "$FLEET_SAVED_STATE" 2> /dev/null | tail -n 1 || true; }

fleet_require_yaml() {
    python3 -c 'import yaml' 2> /dev/null && return 0
    if [[ $EUID -eq 0 ]] && command -v apt-get > /dev/null; then
        echo "Installing python3-yaml (needed to read the fleet file)..." >&2
        DEBIAN_FRONTEND=noninteractive apt-get install -y python3-yaml >&2 || true
    fi
    python3 -c 'import yaml' 2> /dev/null || {
        echo "python3-yaml is missing: sudo apt-get install python3-yaml" >&2
        exit 1
    }
}

# Read the fleet flags (--config, --node, --role, --write-config, --classic, --check)
# and, in fleet mode, load this node's settings from the fleet file. Runs before the
# defaults in config.sh, so the fleet file wins over them; environment variables still
# win over the fleet file (its lines only set what is unset).
fleet_init() {
    local _fleet_args=("$@") _i _fleet_env _fleet_role_args
    for ((_i = 0; _i < ${#_fleet_args[@]}; _i++)); do
        case "${_fleet_args[_i]}" in
            --config) FLEET_FILE="${_fleet_args[_i + 1]:-}" _i=$((_i + 1)) ;;
            --node) FLEET_NODE="${_fleet_args[_i + 1]:-}" _i=$((_i + 1)) ;;
            --role) FLEET_ROLE="${_fleet_args[_i + 1]:-}" _i=$((_i + 1)) ;;
            --write-config) FLEET_WRITE="${_fleet_args[_i + 1]:-}" _i=$((_i + 1)) ;;
            --classic) FLEET_CLASSIC=true ;;
            --check) FLEET_CHECK=true ;;
        esac
    done

    # An install that was deployed from a fleet file keeps using it: --auto, /redeploy and
    # a plain manual run all read the saved copy (so a run without flags can never flip
    # the node's roles). --classic leaves fleet mode.
    if [[ -z "$FLEET_WRITE" && "$FLEET_CLASSIC" != "true" && -z "$FLEET_FILE" \
        && -r "$FLEET_SAVED_FILE" && -r "$FLEET_SAVED_STATE" ]]; then
        FLEET_FILE="$FLEET_SAVED_FILE"
        FLEET_FROM_SAVED=true
        [[ -n "$FLEET_NODE" ]] || FLEET_NODE=$(fleet_state_value node)
        [[ -n "$FLEET_ROLE" ]] || FLEET_ROLE=$(fleet_state_value roles)
    fi

    if [[ -n "$FLEET_FILE" && -z "$FLEET_WRITE" && "$FLEET_CLASSIC" != "true" ]]; then
        if [[ -z "$FLEET_NODE" ]]; then
            echo "--config needs --node NAME: the entry in the file that is this machine" >&2
            exit 2
        fi
        fleet_require_yaml
        _fleet_role_args=()
        [[ -z "$FLEET_ROLE" ]] || _fleet_role_args=(--role "$FLEET_ROLE")
        if [[ "$FLEET_CHECK" == "true" ]]; then
            python3 "$FLEET_TOOL" check "$FLEET_FILE" --node "$FLEET_NODE" "${_fleet_role_args[@]}" || exit $?
            exit 0
        fi
        if ! _fleet_env=$(python3 "$FLEET_TOOL" env "$FLEET_FILE" --node "$FLEET_NODE" "${_fleet_role_args[@]}"); then
            echo "The fleet file was rejected. Nothing has been changed." >&2
            exit 1
        fi
        eval "$_fleet_env"
        FLEET_ACTIVE=true
    fi
}

# Remember the fleet file that just deployed successfully, and which entry is this node,
# so --auto and /redeploy keep using them. Only called after a complete deploy, so a
# rejected or failed file never replaces the last good one.
save_fleet_state() {
    [[ "$FLEET_ACTIVE" == "true" ]] || return 0
    mkdir -p "$APP_DIR"
    if [[ "$FLEET_FROM_SAVED" != "true" ]]; then
        if [[ -f "$FLEET_SAVED_FILE" ]] && ! cmp -s "$FLEET_FILE" "$FLEET_SAVED_FILE"; then
            cp -p "$FLEET_SAVED_FILE" "$FLEET_SAVED_FILE.prev"
        fi
        install -m 644 "$FLEET_FILE" "$FLEET_SAVED_FILE.new"
        mv "$FLEET_SAVED_FILE.new" "$FLEET_SAVED_FILE"
        # a signed registry travels with its signature (registry.json -> registry.sig.json)
        local sig_src="${FLEET_FILE%.*}.sig.json" sig_dst="${FLEET_SAVED_FILE%.*}.sig.json"
        if [[ -f "$sig_src" ]]; then
            install -m 644 "$sig_src" "$sig_dst.new"
            mv "$sig_dst.new" "$sig_dst"
        else
            rm -f "$sig_dst"
        fi
    fi
    {
        echo "node=$FLEET_NODE"
        [[ -z "$FLEET_ROLE" ]] || echo "roles=$FLEET_ROLE"
    } > "$FLEET_SAVED_STATE.new"
    mv "$FLEET_SAVED_STATE.new" "$FLEET_SAVED_STATE"
    info "Fleet file saved for --auto: node $FLEET_NODE"
}

# --classic: forget the saved fleet file. deploy.env keeps the settings it produced.
leave_fleet_mode() {
    if [[ -e "$FLEET_SAVED_FILE" || -e "$FLEET_SAVED_STATE" ]]; then
        rm -f "$FLEET_SAVED_FILE" "$FLEET_SAVED_FILE.prev" "$FLEET_SAVED_STATE"
        info "Left fleet mode: the saved fleet file is gone, the settings it produced stay in $SETTINGS_FILE"
    fi
}

# --write-config FILE --node NAME: a one-node fleet file from what this install
# runs now (deploy.env, .telegram), without secrets.
write_fleet_config() {
    if [[ -z "$FLEET_NODE" ]]; then
        echo "--write-config needs --node NAME (a name for this machine: lowercase letters, digits, hyphens)" >&2
        return 2
    fi
    fleet_require_yaml
    local script_dir cred="" candidate ha="$HA_HOST" line tg_args=()
    script_dir="$EDGE_SCRIPT_DIR"
    for candidate in "${TELEGRAM_CREDENTIALS_FILE:-}" "$script_dir/.telegram" "$APP_DIR/.telegram"; do
        if [[ -n "$candidate" && -f "$candidate" ]]; then
            cred="$candidate"
            break
        fi
    done
    if [[ -n "$cred" ]]; then
        tg_args=(--telegram "$cred")
        # .telegram wins over the defaults, as it does for the running bot
        line=$(grep '^HA_HOST=' "$cred" | tail -n 1 || true)
        [[ -z "$line" ]] || ha=$(echo "${line#HA_HOST=}" | tr -d '\r[:space:]')
    fi
    INSTALL_HOMEASSISTANT="$INSTALL_HOMEASSISTANT" INSTALL_MQTT="$INSTALL_MQTT" \
        ENABLE_HWACCEL="$ENABLE_HWACCEL" TIMEZONE="$TIMEZONE" LOCAL_SUBNET="$LOCAL_SUBNET" \
        EXTERNAL_DRIVE_UUID="$EXTERNAL_DRIVE_UUID" EDGE_STORAGE_DIR="$STORAGE_MOUNT" \
        VICTRON_INSTALL="$VICTRON_INSTALL" VICTRON_ADDRESS="$VICTRON_ADDRESS" \
        RENOGY_INSTALL="$RENOGY_INSTALL" RENOGY_ADDRESS="$RENOGY_ADDRESS" \
        REDEPLOY_REPO="$REDEPLOY_REPO" HA_PORT="$HA_PORT" \
        python3 "$FLEET_TOOL" write --node "$FLEET_NODE" --out "$FLEET_WRITE" \
        "${tg_args[@]}" --ha-host "$ha"
}
