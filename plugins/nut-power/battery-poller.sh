#!/bin/bash
# Battery poller for NUT dummy-ups driver
# Reads laptop battery status and writes NUT-compatible format

set -euo pipefail

DUMMY_FILE="/var/run/nut/laptop.dev"
POLL_INTERVAL="${NUT_POLL_INTERVAL:-30}"

# Find battery and AC adapter
find_power_supply() {
    local type="$1"
    for supply in /sys/class/power_supply/*; do
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
    local bat
    bat=$(find_power_supply "Battery") || return 1
    cat "$bat/capacity" 2>/dev/null || echo "100"
}

get_ac_online() {
    # Check for AC/Mains power supply
    for supply in /sys/class/power_supply/*; do
        local type=""
        [ -f "$supply/type" ] && type=$(cat "$supply/type" 2>/dev/null)
        if [ "$type" = "Mains" ]; then
            cat "$supply/online" 2>/dev/null && return 0
        fi
    done
    # Fallback: check AC* or ADP* patterns
    for supply in /sys/class/power_supply/AC* /sys/class/power_supply/ADP*; do
        if [ -f "$supply/online" ]; then
            cat "$supply/online" 2>/dev/null && return 0
        fi
    done
    echo "1"  # Assume on AC if we can't detect
}

write_nut_status() {
    local capacity="$1"
    local ac_online="$2"
    local status

    if [ "$ac_online" = "1" ]; then
        status="OL"  # Online (on AC power)
    else
        status="OB"  # On Battery
    fi

    # Low battery flag
    local low_battery="${NUT_LOW_BATTERY:-10}"
    if [ "$capacity" -le "$low_battery" ]; then
        status="$status LB"  # Add Low Battery flag
    fi

    # Write NUT dummy-ups format
    cat > "$DUMMY_FILE" << EOF
battery.charge: $capacity
battery.charge.low: $low_battery
ups.status: $status
device.type: ups
ups.mfr: Linux
ups.model: Laptop Battery
EOF
}

# Ensure directory exists
mkdir -p "$(dirname "$DUMMY_FILE")"
chown nut:nut "$(dirname "$DUMMY_FILE")" 2>/dev/null || true

echo "Battery poller started (interval: ${POLL_INTERVAL}s)"

while true; do
    capacity=$(get_battery_capacity)
    ac_online=$(get_ac_online)

    write_nut_status "$capacity" "$ac_online"

    sleep "$POLL_INTERVAL"
done
