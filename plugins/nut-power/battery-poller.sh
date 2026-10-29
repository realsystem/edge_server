#!/bin/bash
# Battery poller for NUT dummy-ups driver
# Reads laptop battery status and writes NUT-compatible format.
#
# NUT (upsmon) shuts the machine down when the UPS reports "OB LB"
# (on battery + low battery). upsmon has no "delay on battery" setting, so the
# configurable delay is implemented here: once the machine has been on battery
# for NUT_SHUTDOWN_DELAY seconds, the LB flag is raised as well.

set -euo pipefail

DUMMY_FILE="${NUT_DUMMY_FILE:-/var/run/nut/laptop.dev}"
POLL_INTERVAL="${NUT_POLL_INTERVAL:-30}"
LOW_BATTERY="${NUT_LOW_BATTERY:-20}"
SHUTDOWN_DELAY="${NUT_SHUTDOWN_DELAY:-600}"   # seconds on battery; 0 disables
POWER_SUPPLY_DIR="${POWER_SUPPLY_DIR:-/sys/class/power_supply}"

# Find a power supply of the given type (e.g. Battery)
find_power_supply() {
    local type="$1"
    local supply
    for supply in "$POWER_SUPPLY_DIR"/*; do
        if [ -f "$supply/type" ]; then
            if grep -qi "$type" "$supply/type" 2>/dev/null; then
                echo "$supply"
                return 0
            fi
        fi
    done
    return 1
}

get_battery_capacity() {
    local bat capacity
    if ! bat=$(find_power_supply "Battery"); then
        echo "100"
        return 0
    fi
    capacity=$(cat "$bat/capacity" 2>/dev/null || echo "100")
    [[ "$capacity" =~ ^[0-9]+$ ]] || capacity=100
    echo "$capacity"
}

get_ac_online() {
    local supply type online
    # Check for AC/Mains power supply
    for supply in "$POWER_SUPPLY_DIR"/*; do
        type=""
        [ -f "$supply/type" ] && type=$(cat "$supply/type" 2>/dev/null)
        if [ "$type" = "Mains" ] && [ -f "$supply/online" ]; then
            online=$(cat "$supply/online" 2>/dev/null || echo 1)
            echo "$online"
            return 0
        fi
    done
    # Fallback: check AC* or ADP* patterns
    for supply in "$POWER_SUPPLY_DIR"/AC* "$POWER_SUPPLY_DIR"/ADP*; do
        if [ -f "$supply/online" ]; then
            online=$(cat "$supply/online" 2>/dev/null || echo 1)
            echo "$online"
            return 0
        fi
    done
    echo "1"  # Assume on AC if we can't detect
}

# Compute the NUT ups.status string.
#   compute_status CAPACITY AC_ONLINE ON_BATTERY_SECONDS
compute_status() {
    local capacity="$1" ac_online="$2" on_batt_secs="$3"
    local status

    if [ "$ac_online" = "1" ]; then
        status="OL"  # Online (on AC power)
    else
        status="OB"  # On Battery
    fi

    if [ "$capacity" -le "$LOW_BATTERY" ]; then
        status="$status LB"
    elif [ "$ac_online" != "1" ] && [ "$SHUTDOWN_DELAY" -gt 0 ] \
        && [ "$on_batt_secs" -ge "$SHUTDOWN_DELAY" ]; then
        status="$status LB"  # On battery for too long: request shutdown
    fi
    echo "$status"
}

write_nut_status() {
    local capacity="$1"
    local ac_online="$2"
    local on_batt_secs="${3:-0}"
    local status charge_status tmp

    status=$(compute_status "$capacity" "$ac_online" "$on_batt_secs")
    if [ "$ac_online" = "1" ]; then
        charge_status="charging"
    else
        charge_status="discharging"
    fi

    # Write atomically so dummy-ups never reads a half-written file
    tmp="${DUMMY_FILE}.tmp"
    cat > "$tmp" << EOF
battery.charge: $capacity
battery.charge.low: $LOW_BATTERY
battery.charge.warning: $((LOW_BATTERY + 10))
battery.runtime: 0
ups.status: $status
ups.load: 0
device.type: ups
ups.mfr: Linux
ups.model: Laptop Battery
battery.type: Li-ion
input.transfer.reason: $charge_status
EOF
    chmod 644 "$tmp"
    mv -f "$tmp" "$DUMMY_FILE"
}

main() {
    # Ensure directory exists
    mkdir -p "$(dirname "$DUMMY_FILE")"
    chown nut:nut "$(dirname "$DUMMY_FILE")" 2>/dev/null || true

    echo "Battery poller started (interval: ${POLL_INTERVAL}s, low: ${LOW_BATTERY}%, delay: ${SHUTDOWN_DELAY}s)"

    local on_batt_since=0 now capacity ac_online on_batt_secs
    while true; do
        capacity=$(get_battery_capacity)
        ac_online=$(get_ac_online)
        now=$(date +%s)

        if [ "$ac_online" = "1" ]; then
            on_batt_since=0
            on_batt_secs=0
        else
            [ "$on_batt_since" -eq 0 ] && on_batt_since=$now
            on_batt_secs=$((now - on_batt_since))
        fi

        write_nut_status "$capacity" "$ac_online" "$on_batt_secs"

        sleep "$POLL_INTERVAL"
    done
}

# Only run the loop when executed (tests source this file)
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
