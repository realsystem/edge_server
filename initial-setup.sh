#!/usr/bin/env bash
#===============================================================================
# Edge Server - Initial Setup
# Run this first on a fresh Ubuntu Server installation
# Then run: sudo ./deploy-edge-server.sh
#===============================================================================

set -euo pipefail

BATCH_MODE="${BATCH_MODE:-false}"

info()    { printf '[OK] %s\n' "$*"; }
warn()    { printf '[WARN] %s\n' "$*"; }

# Must run as root
[[ $EUID -ne 0 ]] && { echo "Run with sudo"; exit 1; }

#-------------------------------------------------------------------------------
# System updates
#-------------------------------------------------------------------------------
info "Updating system packages..."
apt update && apt upgrade -y
apt install -y vim curl wget htop parted

#-------------------------------------------------------------------------------
# Configure static IP (optional)
#-------------------------------------------------------------------------------
configure_ip="n"
if [[ "$BATCH_MODE" != "true" ]]; then
    echo
    read -rp "Configure static IP? [y/N]: " configure_ip
fi
if [[ "$configure_ip" =~ ^[Yy]$ ]]; then
    # Detect primary interface
    iface=$(ip route | awk '/default/ {print $5; exit}')
    current_ip=$(ip -4 addr show "$iface" | awk '/inet / {print $2}')
    gateway=$(ip route | awk '/default/ {print $3; exit}')

    info "Current: interface=$iface, ip=$current_ip, gateway=$gateway"

    read -rp "Static IP address (e.g., 192.168.1.50): " static_ip
    read -rp "Gateway IP [${gateway}]: " new_gateway
    new_gateway="${new_gateway:-$gateway}"

    cat > /etc/netplan/00-static.yaml << EOF
network:
  version: 2
  ethernets:
    ${iface}:
      dhcp4: no
      addresses:
        - ${static_ip}/24
      routes:
        - to: default
          via: ${new_gateway}
      nameservers:
        addresses: [8.8.8.8, 1.1.1.1]
EOF

    chmod 600 /etc/netplan/00-static.yaml
    info "Static IP configured. Will apply after reboot."
    warn "Reconnect via SSH at ${static_ip} after reboot"
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
    echo "  2. Reconnect: ssh ${SUDO_USER:-user}@${static_ip:-<new-ip>}"
fi
echo "  3. Deploy: sudo ./deploy-edge-server.sh"
echo
