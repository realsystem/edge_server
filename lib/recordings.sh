# shellcheck shell=bash
# Where Frigate's recordings live, after it has already been deployed once:
#
#  migrate_recordings   EDGE_STORAGE_DIR (or the drive) changed since the last deploy: move the
#      recordings that are already there to the new folder, instead of leaving them behind and
#      starting an empty archive. The stack is stopped while they are copied, the old folder is
#      kept (delete it when you have looked at the new one). STORAGE_MIGRATE=true does it,
#      false never does, empty asks on a manual run and skips (with a warning) in batch mode.
#
#  share_recordings     FRIGATE_SHARE=true, OpenMediaVault only: publish the recordings folder as
#      an OMV shared folder with a read-only NFS share for the local network, so the footage can
#      be browsed from a Mac or PC (host/omv-share-recordings.py). Nothing is ever un-shared.
#
# Neither can leave the NVR stopped: a failed copy starts the old stack again.

# The recordings folder the current compose file mounts as /media/frigate (empty if none).
current_recordings_dir() {
    local compose="${1:-$APP_DIR/docker-compose.yml}"
    [[ -f "$compose" ]] || return 0
    sed -n 's|^[[:space:]]*-[[:space:]]*\(/[^:[:space:]]*\):/media/frigate$|\1|p' "$compose" | head -n 1
}

# Start the old stack again after a failed move.
recordings_restore_old_stack() {
    (cd "$APP_DIR" && docker compose start &> /dev/null) || true
    systemctl start edge-server-health.timer &> /dev/null || true
}

migrate_recordings() {
    local old new want="${STORAGE_MIGRATE:-}" size_kb avail_kb probe

    old=$(current_recordings_dir)
    old="${old%/}"
    new="${STORAGE_MOUNT%/}/frigate"
    [[ -n "$old" && "$old" != "$new" ]] || return 0

    if [[ ! -d "$old/recordings" ]] || [[ -z "$(ls -A "$old/recordings" 2> /dev/null)" ]]; then
        info "Recordings folder changes from $old to $new; the old one has no recordings to move"
        return 0
    fi
    case "$new/" in
        "$old"/*)
            warn "The new recordings folder $new is inside the old one $old: not moving anything"
            return 0
            ;;
    esac
    case "$old/" in
        "$new"/*)
            warn "The old recordings folder $old is inside the new one $new: not moving anything"
            return 0
            ;;
    esac

    size_kb=$(du -sk "$old" 2> /dev/null | cut -f1) || size_kb=0
    info "Recordings folder changes from $old to $new ($((size_kb / 1048576)) GB of recordings in the old one)"
    case "$want" in
        true) ;;
        false)
            info "STORAGE_MIGRATE=false: the old recordings stay in $old"
            return 0
            ;;
        "")
            if [[ "$BATCH_MODE" == "true" ]]; then
                warn "The old recordings stay in $old (set STORAGE_MIGRATE=true to move them to $new; Frigate is stopped while they are copied)"
                return 0
            fi
            echo ""
            echo "Frigate will be stopped while $((size_kb / 1048576)) GB are copied to $new (about 5-10 minutes per 10 GB on a hard disk)."
            if ! confirm "Move the existing recordings to $new?"; then
                info "The old recordings stay in $old"
                return 0
            fi
            ;;
        *)
            warn "STORAGE_MIGRATE must be true or false, not '$want': the old recordings stay in $old"
            return 0
            ;;
    esac
    if ! command -v rsync &> /dev/null; then
        warn "rsync is not installed: the old recordings stay in $old (install rsync and run the deploy again)"
        return 0
    fi

    mkdir -p "$new"
    probe="$new"
    avail_kb=$(df --output=avail -k "$probe" 2> /dev/null | tail -n 1 | tr -d ' ') || avail_kb=0
    if [[ ! "$avail_kb" =~ ^[0-9]+$ || "$avail_kb" -lt $((size_kb + size_kb / 20)) ]]; then
        warn "Not enough free space in $new for the recordings in $old: they stay where they are"
        return 0
    fi

    info "Stopping the stack and copying the recordings to $new..."
    # the health timer would start the stopped containers again, with the old folder
    systemctl stop edge-server-health.timer &> /dev/null || true
    (cd "$APP_DIR" && docker compose stop -t 60) || warn "Could not stop the stack cleanly"
    if ! rsync -aH --delete "$old/" "$new/"; then
        recordings_restore_old_stack
        fatal "Copying the recordings failed: nothing was changed in $old and the old stack was started again. Fix the cause (space? disk errors?) and run the deploy again."
    fi
    if [[ -n "$(rsync -an --delete --itemize-changes "$old/" "$new/" 2> /dev/null | head -n 1)" ]]; then
        recordings_restore_old_stack
        fatal "The copy in $new does not match $old: the old stack was started again, $old is untouched. Run the deploy again."
    fi
    success "Recordings copied to $new; the old copy in $old is kept (delete it once you have checked the new one)"
}

share_recordings() {
    local want="${FRIGATE_SHARE:-false}" client="${FRIGATE_SHARE_CLIENT:-}" dir="${STORAGE_MOUNT%/}/frigate" found out script_dir
    script_dir="$EDGE_SCRIPT_DIR"

    case "$want" in
        true) ;;
        false | "") return 0 ;;
        *)
            warn "FRIGATE_SHARE must be true or false, not '$want': recordings are not shared"
            return 0
            ;;
    esac
    if ! platform_is_omv; then
        info "FRIGATE_SHARE skipped: it publishes through OpenMediaVault, which is not on this machine"
        return 0
    fi
    if [[ ! -f "$script_dir/host/omv-share-recordings.py" ]] || ! command -v python3 &> /dev/null; then
        warn "host/omv-share-recordings.py or python3 not found: recordings are not shared"
        return 0
    fi
    if [[ -z "$client" ]]; then
        if [[ -n "${LOCAL_SUBNET:-}" ]]; then
            client="$LOCAL_SUBNET"
        elif found=$(detect_local_subnet 2> /dev/null); then
            client="${found%% *}"
        else
            warn "Cannot tell the local network: set FRIGATE_SHARE_CLIENT=10.0.0.0/24. Recordings are not shared"
            return 0
        fi
    fi
    if [[ ! "$client" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/([0-9]|[12][0-9]|3[0-2]))?$ ]]; then
        warn "FRIGATE_SHARE_CLIENT '$client' is not an IPv4 address or network like 10.0.0.0/24: recordings are not shared"
        return 0
    fi

    info "Publishing $dir over NFS (read-only, for $client)..."
    if out=$(python3 "$script_dir/host/omv-share-recordings.py" --path "$dir" --client "$client" 2>&1); then
        while IFS= read -r line; do info "  $line"; done <<< "$out"
        success "Recordings shared: nfs://<this machine>/export/frigate"
    else
        warn "Could not share the recordings: $out"
    fi
}
