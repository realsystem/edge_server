#!/usr/bin/env bash
#===============================================================================
# Edge Server Deployment Script
# Idempotent provisioning for Ubuntu Server and Debian (OpenMediaVault included)
#
# Usage: sudo ./deploy-edge-server.sh           deploy or update by hand
#        sudo ./deploy-edge-server.sh --auto    unattended update, see lib/redeploy.sh
#
# Components: Frigate NVR, Tailscale
#   optional: Home Assistant (INSTALL_HOMEASSISTANT=true),
#             Mosquitto MQTT (INSTALL_MQTT=true),
#             Telegram bot (installed when a .telegram file exists)
#
# This file is the outline: it loads lib/ and runs the steps in order (run_deploy).
# The steps themselves live in lib/, one file per area:
#   fleet.sh settings.sh config.sh   where settings come from, and their defaults
#   common.sh                        logging and small helpers
#   os.sh os/ubuntu.sh os/debian.sh  what differs between distributions
#   host.sh                          laptop or server, and the power settings for it
#   preflight.sh packages.sh network.sh storage.sh
#   stack.sh control-plane.sh telegram.sh services.sh plugins.sh redeploy.sh
#===============================================================================

set -euo pipefail
IFS=$'\n\t'

EDGE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDGE_LIB_DIR="$EDGE_SCRIPT_DIR/lib"

# --ha-node: configure a Home Assistant OS node over the LAN (this machine is the jumpbox).
# Nothing here installs or changes this server; see docs/design/ha-node.md.
if [[ "${1:-}" == "--ha-node" ]]; then
    exec python3 "$EDGE_SCRIPT_DIR/tools/ha_node.py" wizard "${@:2}"
fi

if [[ ! -r "$EDGE_LIB_DIR/common.sh" ]]; then
    echo "lib/ is missing next to ${BASH_SOURCE[0]}: copy the whole edge_server folder, not just this script" >&2
    exit 1
fi

#-------------------------------------------------------------------------------
# SETTINGS: first of command-line flags, environment, fleet file, deploy.env of the
# last run (--auto), built-in defaults. The order of these three lines is that order.
#-------------------------------------------------------------------------------
# shellcheck source=lib/fleet.sh
source "$EDGE_LIB_DIR/fleet.sh"
fleet_init "$@"
# shellcheck source=lib/settings.sh
source "$EDGE_LIB_DIR/settings.sh"
load_saved_settings "$@"
# shellcheck source=lib/config.sh
source "$EDGE_LIB_DIR/config.sh"

#-------------------------------------------------------------------------------
# STEPS (functions only: loading them changes nothing)
#-------------------------------------------------------------------------------
for _lib in common os host preflight packages network storage \
    control-plane telegram stack services plugins redeploy; do
    # shellcheck source=/dev/null
    source "$EDGE_LIB_DIR/$_lib.sh"
done
unset _lib

#-------------------------------------------------------------------------------
# MAIN
#-------------------------------------------------------------------------------

usage() {
    cat << 'EOF'
Usage: sudo ./deploy-edge-server.sh [--auto | --uninstall]
       sudo ./deploy-edge-server.sh --config fleet.yaml --node NAME [--role LIST]
       ./deploy-edge-server.sh --config fleet.yaml --node NAME --check
       sudo ./deploy-edge-server.sh --write-config fleet.yaml --node NAME
       sudo ./deploy-edge-server.sh --classic
       ./deploy-edge-server.sh --ha-node [--site FILE] [--yes]

  (no option)  deploy or update this server, asking questions where needed
  --auto       unattended: fetch the newest release tag (v1.0, v1.2.3; nothing
               else is ever deployed) from GitHub, require green CI,
               deploy it with the settings of the last manual run, check that
               everything comes up and roll back if not. The Telegram bot's
               /redeploy command runs this.
  --uninstall  remove add-ons (Victron Smart Shunt, Renogy Rover, power loss
               monitor), asking about each one. Everything else stays. Not
               combinable with --auto.
  --config F   fleet mode: settings come from this node's entry in the fleet file
               (examples/fleet.*.yaml, docs/design/edge-system.md). The file is checked
               first; an invalid one changes nothing. After a successful deploy it is
               saved and used by --auto and by later runs without flags.
  --node NAME  which entry of the fleet file is this machine (with --config)
  --role LIST  roles for this run instead of the node's (nvr,power,ha)
  --check      with --config: validate the file and print what it would deploy, then stop
  --write-config F  write a one-node fleet file from this install's current settings
  --classic    leave fleet mode: forget the saved fleet file, settings stay as deployed
  --ha-node    configure a Home Assistant OS node (a Raspberry Pi) from this machine:
               asks for its address and what to deploy, shows the plan, backs the node
               up, copies only files it owns, checks the configuration and restores the
               old files if it is invalid. Does not change this server. --yes repeats the saved
               answers without asking. See docs/design/ha-node.md.
  -h, --help   this text
EOF
}

run_deploy() {
    info "Starting deployment at $(date)"
    info "Log file: $LOG_FILE"
    if [[ "$FLEET_ACTIVE" == "true" ]]; then
        info "Fleet mode: node $FLEET_NODE at site $EDGE_SITE, roles ${FLEET_ROLES:-?}"
        case ",${FLEET_ROLES:-}," in
            *,archive,* | *,agent,*) warn "The archive and agent roles are not implemented in this release and are ignored" ;;
        esac
    fi

    preflight_checks
    resolve_local_subnet # before anything changes: fail early if the network is unknown
    configure_system
    install_packages
    install_docker
    install_tailscale
    configure_firewall
    configure_storage
    prepare_telegram_bot
    prepare_processor
    create_app_stack
    discover_cameras
    create_systemd_service
    install_reboot_unit
    install_auto_redeploy
    install_service_requests
    install_host_helpers
    start_services
    install_victron
    install_nut_power
    install_renogy
    save_settings
    save_fleet_state
    show_summary

    info "Deployment completed successfully at $(date)"
}

main() {
    local mode=manual uninstall=false
    while (($# > 0)); do
        case "$1" in
            --auto) mode=auto ;;
            --auto-apply) mode=apply ;;
            --uninstall) uninstall=true ;;
            --rolled-back) ROLLED_BACK=true ;;
            --config | --node | --role | --write-config)
                if (($# < 2)); then
                    echo "$1 needs a value" >&2
                    return 2
                fi
                shift # the value was read by the fleet block at the top of the file
                ;;
            --classic | --check) ;;
            --ha-node) ;; # handled at the top of the file
            -h | --help)
                usage
                return 0
                ;;
            *)
                echo "Unknown option: $1" >&2
                usage >&2
                return 2
                ;;
        esac
        shift
    done

    if [[ "$mode" != "manual" && ( -n "$FLEET_WRITE" || "$FLEET_CLASSIC" == "true" || "$FLEET_CHECK" == "true" ) ]]; then
        echo "--write-config, --classic and --check are for manual runs and cannot be combined with --auto" >&2
        return 2
    fi
    if [[ "$mode" != "manual" && -n "$FLEET_FILE" && "$FLEET_FROM_SAVED" != "true" ]]; then
        echo "--auto uses the fleet file saved by the last manual deploy; --config is for manual runs" >&2
        return 2
    fi
    if [[ "$FLEET_CHECK" == "true" && -z "$FLEET_FILE" ]]; then
        echo "--check needs --config FILE --node NAME" >&2
        return 2
    fi
    if [[ -n "$FLEET_WRITE" ]]; then
        if [[ -n "$FLEET_FILE" || "$uninstall" == "true" ]]; then
            echo "--write-config cannot be combined with --config or --uninstall" >&2
            return 2
        fi
        write_fleet_config
        return $?
    fi
    if [[ "$FLEET_CLASSIC" == "true" && -n "$FLEET_FILE" ]]; then
        echo "--classic and --config exclude each other" >&2
        return 2
    fi

    if [[ "$uninstall" == "true" && "$mode" != "manual" ]]; then
        echo "--uninstall is for manual runs and cannot be combined with --auto" >&2
        return 2
    fi

    echo
    echo "=============================================================================="
    echo "Edge Server Deployment Script"
    echo "=============================================================================="
    echo

    # Root check before anything touches /var/log
    if [[ $EUID -ne 0 ]]; then
        echo "This script must be run as root (use sudo)" >&2
        exit 1
    fi

    # Initialize log
    mkdir -p "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE"
    chmod 644 "$LOG_FILE"

    if [[ "$FLEET_CLASSIC" == "true" ]]; then
        leave_fleet_mode
    fi

    if [[ "$uninstall" == "true" ]]; then
        uninstall_addons
        return $?
    fi

    case "$mode" in
        manual) run_deploy ;;
        auto)
            BATCH_MODE=true
            auto_update
            ;;
        apply)
            BATCH_MODE=true
            auto_apply
            ;;
    esac
}

# Only run when executed, so tests can `source` this file and call functions.
# "exit" matters: a later --auto replaces this file while bash may still be reading it.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
    exit $?
fi
