#!/usr/bin/env bash
#===============================================================================
# Edge Server - Initial Setup
# Run this first on a fresh Ubuntu Server installation
# Then run: sudo ./deploy-edge-server.sh
#===============================================================================

set -euo pipefail

BATCH_MODE="${BATCH_MODE:-false}"

# Optional non-interactive network config (set by bootstrap):
#   STATIC_IP=192.168.1.50[/24]  GATEWAY=192.168.1.1  DNS=1.1.1.1
STATIC_IP="${STATIC_IP:-}"
GATEWAY="${GATEWAY:-}"
DNS="${DNS:-}"

export DEBIAN_FRONTEND=noninteractive

info()    { printf '[OK] %s\n' "$*"; }
warn()    { printf '[WARN] %s\n' "$*"; }

# Must run as root
[[ $EUID -ne 0 ]] && { echo "Run with sudo"; exit 1; }

#-------------------------------------------------------------------------------
# System updates
#-------------------------------------------------------------------------------
info "Updating system packages..."
apt-get update
apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
apt-get install -y vim curl wget htop parted

#-------------------------------------------------------------------------------
# Configure static IP (optional)
#-------------------------------------------------------------------------------
configure_ip="n"
static_ip=""
new_gateway=""
if [[ -n "$STATIC_IP" ]]; then
    configure_ip="y"
elif [[ "$BATCH_MODE" != "true" ]]; then
    echo
    read -rp "Configure static IP? [y/N]: " configure_ip
fi
if [[ "$configure_ip" =~ ^[Yy]$ ]]; then
    # Detect primary interface
    iface=$(ip route | awk '/default/ {print $5; exit}')
    if [[ -z "$iface" ]]; then
        warn "Could not detect the primary network interface - skipping static IP"
        configure_ip="n"
    fi
fi
if [[ "$configure_ip" =~ ^[Yy]$ ]]; then
    current_ip=$(ip -4 addr show "$iface" | awk '/inet / {print $2; exit}')
    gateway=$(ip route | awk '/default/ {print $3; exit}')

    info "Current: interface=$iface, ip=$current_ip, gateway=$gateway"

    if [[ -n "$STATIC_IP" ]]; then
        static_ip="$STATIC_IP"
        new_gateway="${GATEWAY:-$gateway}"
    else
        read -rp "Static IP address (e.g., 192.168.1.50 or 192.168.1.50/24): " static_ip
        read -rp "Gateway IP [${gateway}]: " new_gateway
        new_gateway="${new_gateway:-$gateway}"
    fi

    # Accept both "a.b.c.d" and "a.b.c.d/nn"; default to /24
    [[ "$static_ip" == */* ]] || static_ip="${static_ip}/24"
    if [[ ! "$static_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]]; then
        echo "Invalid static IP: $static_ip"
        exit 1
    fi
    if [[ ! "$new_gateway" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        echo "Invalid gateway: $new_gateway"
        exit 1
    fi

    if [[ -n "$DNS" ]]; then
        dns_servers="${DNS}, 1.1.1.1"
    else
        dns_servers="8.8.8.8, 1.1.1.1"
    fi

    cat > /etc/netplan/00-static.yaml << EOF
network:
  version: 2
  ethernets:
    ${iface}:
      dhcp4: no
      addresses:
        - ${static_ip}
      routes:
        - to: default
          via: ${new_gateway}
      nameservers:
        addresses: [${dns_servers}]
EOF

    chmod 600 /etc/netplan/00-static.yaml
    info "Static IP configured. Will apply after reboot."
    warn "Reconnect via SSH at ${static_ip%/*} after reboot"
fi

#-------------------------------------------------------------------------------
# Prepare external storage
#-------------------------------------------------------------------------------
format_drive="n"
if [[ "$BATCH_MODE" != "true" ]]; then
    echo
    info "Available block devices:"
    lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,UUID | grep -v "loop"
    echo
    read -rp "Format external drive for Frigate storage? [y/N]: " format_drive
fi
if [[ "$format_drive" =~ ^[Yy]$ ]]; then
    read -rp "Enter device name (e.g., sdb): " drive_name
    drive="/dev/${drive_name}"

    [[ ! -b "$drive" ]] && { echo "Device $drive not found"; exit 1; }

    warn "This will ERASE all data on $drive"
    read -rp "Type 'yes' to confirm: " confirm
    [[ "$confirm" != "yes" ]] && { echo "Aborted"; exit 1; }

    info "Creating partition table..."
    parted "$drive" --script mklabel gpt
    parted "$drive" --script mkpart primary ext4 0% 100%

    sleep 2
    partition="${drive}1"

    info "Formatting as ext4..."
    mkfs.ext4 -L nvr-storage "$partition"

    uuid=$(blkid -s UUID -o value "$partition")
    info "Drive UUID: $uuid"
    echo
    echo "Save this for deploy-edge-server.sh:"
    echo "  export EXTERNAL_DRIVE_UUID=\"$uuid\""
    echo
fi

#-------------------------------------------------------------------------------
# Show drive UUIDs
#-------------------------------------------------------------------------------
if [[ "$BATCH_MODE" != "true" ]]; then
    echo
    info "External drive UUIDs (for deploy-edge-server.sh):"
    blkid | grep -v "loop" | grep -v "$(df / | tail -1 | awk '{print $1}')" || true
fi

#-------------------------------------------------------------------------------
# Done
#-------------------------------------------------------------------------------
echo
info "Initial setup complete!"
echo
echo "Next steps:"
echo "  1. Reboot: sudo reboot"
if [[ "$configure_ip" =~ ^[Yy]$ ]]; then
    echo "  2. Reconnect: ssh ${SUDO_USER:-user}@${static_ip%/*}"
fi
echo "  3. Deploy: sudo ./deploy-edge-server.sh"
echo
