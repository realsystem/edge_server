# shellcheck shell=bash
# deploy.env: what a manual deploy saved, so --auto (the Telegram bot's /redeploy)
# can repeat it without anyone present.

SETTINGS_FILE="${EDGE_SERVER_DIR:-/opt/edge-server}/deploy.env"

# Settings saved by a manual run become the defaults for --auto. Called before the
# defaults in config.sh are assigned; environment variables still win.
load_saved_settings() {
    local _arg
    for _arg in "$@"; do
        # --classic after fleet mode keeps the settings the fleet file produced; --write-config
        # describes the install as it was last deployed
        if [[ "$_arg" == "--auto" || "$_arg" == "--auto-apply" || "$_arg" == "--write-config" \
            || ( "$_arg" == "--classic" && -r "$FLEET_SAVED_FILE" ) ]] && [[ -r "$SETTINGS_FILE" ]]; then
            # shellcheck source=/dev/null
            source "$SETTINGS_FILE"
            break
        fi
    done
}

# Everything a later --auto run needs to repeat this deployment. Not the
# Tailscale key (already authenticated). The file is root-only: it holds the
# MQTT password and BLE keys.
SAVED_SETTINGS=(
    INSTALL_HOMEASSISTANT INSTALL_MQTT MQTT_USER MQTT_PASS ENABLE_HWACCEL DRI_DEVICE
    TIMEZONE EXTERNAL_DRIVE_UUID FRIGATE_IMAGE HOST_TYPE HOST_PLATFORM
    NUT_POWER_INSTALL NUT_SHUTDOWN_DELAY NUT_LOW_BATTERY
    VICTRON_INSTALL VICTRON_ADDRESS VICTRON_KEY RENOGY_INSTALL RENOGY_ADDRESS
    REDEPLOY_REPO HA_HOST HA_PORT
    INSTALL_PROCESSOR PROCESSOR_IMAGE PROCESSOR_PORT PROCESSOR_BIND PROCESSOR_ADMIN_EMAIL
    PROCESSOR_WORKER_AUTH
)

save_settings() {
    local tmp name
    mkdir -p "$APP_DIR"
    tmp=$(umask 077 && mktemp "$SETTINGS_FILE.XXXXXX")
    {
        echo "# Written by deploy-edge-server.sh for --auto. Environment variables win."
        for name in "${SAVED_SETTINGS[@]}"; do
            printf '[[ -v %s ]] || %s=%q\n' "$name" "$name" "${!name:-}"
        done
        printf '[[ -v INSTALL_TELEGRAM_BOT ]] || INSTALL_TELEGRAM_BOT=%q\n' "$INSTALL_TELEGRAM_BOT_REQUEST"
        printf '[[ -v EDGE_STORAGE_DIR ]] || EDGE_STORAGE_DIR=%q\n' "$STORAGE_MOUNT"
        # as requested, not as detected: empty keeps following the network the server is on
        printf '[[ -v LOCAL_SUBNET ]] || LOCAL_SUBNET=%q\n' "$LOCAL_SUBNET_REQUEST"
    } > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$SETTINGS_FILE"
}
