# shellcheck shell=bash
# Where Frigate keeps its recordings: an external drive (fstab entry and mount), or on
# OpenMediaVault a folder on a file system OMV mounts (lib/platform.sh).

configure_storage() {
    info "Configuring external storage..."

    # Create mount point
    mkdir -p "$STORAGE_MOUNT"

    if platform_is_omv; then
        # OMV mounts its drives and owns /etc/fstab: no entry, no mount, no formatting here
        mkdir -p "$STORAGE_MOUNT/frigate/recordings" "$STORAGE_MOUNT/frigate/clips"
        info "OpenMediaVault: drive mounts and /etc/fstab left to OMV"
        success "Storage at $STORAGE_MOUNT"
        return 0
    fi

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

    if grep -q "UUID=${EXTERNAL_DRIVE_UUID}" "$FSTAB_FILE"; then
        info "fstab entry already exists for UUID ${EXTERNAL_DRIVE_UUID}"
    else
        info "Adding fstab entry..."
        echo "# External storage for Frigate NVR" >> "$FSTAB_FILE"
        echo "$fstab_entry" >> "$FSTAB_FILE"
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
