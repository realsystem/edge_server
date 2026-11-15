# shellcheck shell=bash
# The external drive for Frigate recordings (fstab entry and mount).

configure_storage() {
    info "Configuring external storage..."

    # Create mount point
    mkdir -p "$STORAGE_MOUNT"

    if [[ -z "$EXTERNAL_DRIVE_UUID" ]]; then
        warn "No external drive UUID specified - skipping storage configuration"
        warn "Video recording will use local storage at $STORAGE_MOUNT"
        return 0
    fi

    # Verify the UUID exists
    if ! blkid -U "$EXTERNAL_DRIVE_UUID" &> /dev/null; then
        warn "Drive with UUID $EXTERNAL_DRIVE_UUID not found - skipping fstab entry"
        return 0
    fi

    # Add fstab entry if not present
    local fstab_entry="UUID=${EXTERNAL_DRIVE_UUID} ${STORAGE_MOUNT} auto defaults,nofail,x-systemd.device-timeout=10s,x-systemd.mount-timeout=10s 0 2"

    if grep -q "UUID=${EXTERNAL_DRIVE_UUID}" /etc/fstab; then
        info "fstab entry already exists for UUID ${EXTERNAL_DRIVE_UUID}"
    else
        info "Adding fstab entry..."
        echo "# External storage for Frigate NVR" >> /etc/fstab
        echo "$fstab_entry" >> /etc/fstab
    fi

    # Mount if not already mounted
    if ! mountpoint -q "$STORAGE_MOUNT"; then
        mount "$STORAGE_MOUNT" || warn "Could not mount $STORAGE_MOUNT - will retry on reboot"
    fi

    # Create Frigate directories
    mkdir -p "$STORAGE_MOUNT/frigate/recordings"
    mkdir -p "$STORAGE_MOUNT/frigate/clips"

    success "Storage configured at $STORAGE_MOUNT"
}
