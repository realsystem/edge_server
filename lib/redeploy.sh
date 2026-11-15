# shellcheck shell=bash
# --auto: redeploy without anyone present (the Telegram bot's /redeploy runs this).
#   1. fetch the newest release tag (vMAJOR.MINOR or vMAJOR.MINOR.PATCH, nothing
#      else) from GitHub (REDEPLOY_REPO) into $APP_DIR/source and require green
#      CI on it
#   2. deploy it with the settings saved by the last manual deploy ($APP_DIR/deploy.env)
#   3. check that every container and Frigate come up; if not, go back to the
#      previous version and deploy that
#   4. write the outcome to $APP_DIR/bot-requests/redeploy-result for the bot

# Host side of /redeploy: a path unit that runs "deploy-edge-server.sh --auto"
# when the bot drops bot-requests/redeploy. Needs the source checkout.
install_auto_redeploy() {
    local dir="$SYSTEMD_DIR"
    if [[ "$TELEGRAM_REBOOT_ENABLED" != "true" ]]; then
        if [[ -f "$dir/edge-redeploy.path" || -f "$dir/edge-redeploy.service" ]]; then
            systemctl disable --now edge-redeploy.path &> /dev/null || true
            rm -f "$dir/edge-redeploy.path" "$dir/edge-redeploy.service"
            systemctl daemon-reload
            info "Removed the redeploy watcher (no REBOOT_TOTP_SECRET)"
        fi
        return 0
    fi
    if ! ensure_source_checkout; then
        warn "Could not get $REDEPLOY_REPO - /redeploy is not available (the rest is unaffected)"
        return 0
    fi

    info "Installing the Telegram /redeploy watcher..."
    mkdir -p "$dir"
    cat > "$dir/edge-redeploy.path" << UNIT
[Unit]
Description=Redeploy request from the Telegram bot

[Path]
PathExists=$APP_DIR/bot-requests/redeploy
Unit=edge-redeploy.service

[Install]
WantedBy=multi-user.target
UNIT
    cat > "$dir/edge-redeploy.service" << UNIT
[Unit]
Description=Update from GitHub and redeploy (requested from the Telegram bot)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
Environment=EDGE_SERVER_DIR=$APP_DIR
TimeoutStartSec=2400
# Consume the request first so it can never loop
ExecStartPre=/bin/rm -f $APP_DIR/bot-requests/redeploy
ExecStart=$SOURCE_DIR/deploy-edge-server.sh --auto
UNIT
    chmod 644 "$dir/edge-redeploy.path" "$dir/edge-redeploy.service"
    rm -f "$APP_DIR/bot-requests/redeploy"
    systemctl daemon-reload
    systemctl enable --now edge-redeploy.path
}

# The checkout --auto deploys from. Created by the first manual deploy.
ensure_source_checkout() {
    if [[ -d "$SOURCE_DIR/.git" ]]; then
        git -C "$SOURCE_DIR" remote set-url origin "$REDEPLOY_REPO"
        return 0
    fi
    rm -rf "$SOURCE_DIR"
    git clone --quiet "$REDEPLOY_REPO" "$SOURCE_DIR" \
        && [[ -f "$SOURCE_DIR/deploy-edge-server.sh" ]]
}

# Highest release tag in the source checkout, or nothing. Only names matching
# RELEASE_TAG_REGEX count, so a branch, a commit id or "v1.0-rc1" can never be picked.
latest_release_tag() {
    local tag
    git -C "$SOURCE_DIR" tag --list 'v*' --sort=-v:refname 2> /dev/null \
        | while IFS= read -r tag; do
            if [[ "$tag" =~ $RELEASE_TAG_REGEX ]]; then
                echo "$tag"
                break
            fi
        done
}

# Tell the Telegram bot how it went. Plain text only: it ends up inside JSON.
write_redeploy_result() {
    local status="$1" from="${2:-}" to="${3:-}" detail="${4:-}" tag="${5:-${REDEPLOY_TAG:-}}"
    local dir="$APP_DIR/bot-requests" tmp
    [[ -d "$dir" ]] || return 0
    from=${from//[^0-9a-f]/}
    to=${to//[^0-9a-f]/}
    tag=${tag//[^0-9.v]/}
    detail=${detail//[^[:alnum:] .,:;()_\/-]/ }
    tmp=$(mktemp "$dir/.redeploy-result.XXXXXX")
    printf '{"status": "%s", "from": "%s", "to": "%s", "tag": "%s", "detail": "%s", "at": %s}\n' \
        "$status" "$from" "$to" "$tag" "$detail" "$(date +%s)" > "$tmp"
    chmod 644 "$tmp"
    mv "$tmp" "$dir/redeploy-result"
}

# State of the GitHub check runs of a commit: success, pending, failure, none or unreachable
ci_state() {
    local sha="$1" url slug json
    url=$(git -C "$SOURCE_DIR" config --get remote.origin.url 2> /dev/null) || url=""
    slug=${url#https://github.com/}
    slug=${slug#git@github.com:}
    slug=${slug%.git}
    [[ "$slug" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || {
        echo "unsupported"
        return 0
    }
    if ! json=$(curl -fsS -m 20 -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/repos/$slug/commits/$sha/check-runs?per_page=100" 2> /dev/null); then
        echo "unreachable"
        return 0
    fi
    python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin)["check_runs"]
except (ValueError, KeyError, TypeError):
    print("unreachable")
    raise SystemExit
if not runs:
    print("none")
elif any(r.get("status") != "completed" for r in runs):
    print("pending")
elif any(r.get("conclusion") not in ("success", "skipped", "neutral") for r in runs):
    print("failure")
else:
    print("success")
' <<< "$json"
}

# Copy of what a rollback or a mistake would need, newest five kept
backup_for_redeploy() {
    local dir="$APP_DIR/backups" stamp
    stamp=$(date +%Y%m%d-%H%M%S)
    mkdir -p "$dir"
    chmod 700 "$dir"
    (cd "$APP_DIR" && tar -czf "$dir/redeploy-$stamp.tgz" --ignore-failed-read \
        docker-compose.yml frigate/config.yml .cam .telegram deploy.env 2> /dev/null) \
        || warn "Could not back up the configuration"
    chmod 600 "$dir"/redeploy-*.tgz 2> /dev/null || true
    # shellcheck disable=SC2012
    ls -1t "$dir"/redeploy-*.tgz 2> /dev/null | tail -n +6 | xargs -r rm -f
}

# True once every container runs and Frigate answers, false after VERIFY_TIMEOUT seconds
verify_stack() {
    local -a names
    local name ok deadline=$((SECONDS + VERIFY_TIMEOUT))
    IFS=' ' read -ra names <<< "$(stack_containers)"
    while ((SECONDS < deadline)); do
        ok=true
        for name in "${names[@]}"; do
            [[ "$(docker inspect -f '{{.State.Status}}' "$name" 2> /dev/null)" == "running" ]] || ok=false
        done
        if [[ "$ok" == "true" ]] && curl -fs -m 5 http://127.0.0.1:5000/api/version > /dev/null 2>&1; then
            return 0
        fi
        sleep "${VERIFY_POLL:-5}"
    done
    return 1
}

# One redeploy at a time, across the exec into the new version
take_redeploy_lock() {
    [[ -n "${REDEPLOY_LOCKED:-}" ]] && return 0
    mkdir -p "$(dirname "$REDEPLOY_LOCK_FILE")"
    exec 9> "$REDEPLOY_LOCK_FILE"
    flock -n 9 || return 1
    export REDEPLOY_LOCKED=1
}

# Phase 1: fetch, gate, back up, then hand over to the freshly fetched script
auto_update() {
    local prev new state tag
    if ! take_redeploy_lock; then
        write_redeploy_result refused "" "" "another deploy is already running"
        fatal "Another deploy is running"
    fi
    if [[ ! -r "$SETTINGS_FILE" ]]; then
        write_redeploy_result refused "" "" "no saved settings - run deploy-edge-server.sh by hand once"
        fatal "No $SETTINGS_FILE: run the deploy script by hand once first"
    fi
    if [[ ! -d "$SOURCE_DIR/.git" ]]; then
        write_redeploy_result refused "" "" "no source checkout - run deploy-edge-server.sh by hand once"
        fatal "No source checkout at $SOURCE_DIR: run the deploy script by hand once first"
    fi

    prev=$(git -C "$SOURCE_DIR" rev-parse HEAD)
    info "Fetching release tags from $REDEPLOY_REPO..."
    git -C "$SOURCE_DIR" remote set-url origin "$REDEPLOY_REPO"
    # tags only: no branch is ever fetched for deploying
    if ! GIT_TERMINAL_PROMPT=0 timeout 180 git -C "$SOURCE_DIR" fetch --quiet --force --no-tags origin \
        '+refs/tags/*:refs/tags/*'; then
        write_redeploy_result refused "$prev" "" "cannot fetch from GitHub"
        fatal "git fetch failed"
    fi
    tag=$(latest_release_tag)
    if [[ -z "$tag" ]]; then
        write_redeploy_result refused "$prev" "" "no release tag (like v1.0) exists on GitHub yet"
        fatal "No release tag matching $RELEASE_TAG_REGEX in $REDEPLOY_REPO"
    fi
    new=$(git -C "$SOURCE_DIR" rev-parse "refs/tags/$tag^{commit}")
    info "Current ${prev:0:7}, newest release $tag (${new:0:7})"
    export REDEPLOY_TAG="$tag"

    if [[ "$new" != "$prev" && "$REDEPLOY_REQUIRE_CI" == "true" ]]; then
        state=$(ci_state "$new")
        info "CI on ${new:0:7}: $state"
        if [[ "$state" != "success" ]]; then
            local why
            case "$state" in
                pending) why="CI is still running on the new version, try again in a few minutes" ;;
                failure) why="CI failed on the new version" ;;
                none) why="no CI results for the new version yet" ;;
                *) why="cannot check CI on GitHub ($state)" ;;
            esac
            write_redeploy_result refused "$prev" "$new" "$why"
            fatal "Not deploying ${new:0:7}: $why"
        fi
    fi

    backup_for_redeploy
    git -C "$SOURCE_DIR" reset --quiet --hard "$new"
    info "Handing over to $tag (${new:0:7})"
    # exec: this process must not read another byte of a script that was just replaced
    REDEPLOY_FROM="$prev" REDEPLOY_TO="$new" exec "$SOURCE_DIR/deploy-edge-server.sh" --auto-apply
}

# Phase 2: deploy what is checked out, verify it, fall back to the previous version if it fails
auto_apply() {
    local from="${REDEPLOY_FROM:-}" to="${REDEPLOY_TO:-}" head rc=0 detail=""
    take_redeploy_lock || fatal "Another deploy is running"
    head=$(git -C "$SOURCE_DIR" rev-parse HEAD 2> /dev/null || echo "")
    [[ -n "$from" ]] || from="$head"
    [[ -n "$to" ]] || to="$head"
    info "Auto deploy of ${head:0:7} (from ${from:0:7})"

    set +e
    (
        set -e
        run_deploy
    )
    rc=$?
    set -e
    if ((rc != 0)); then
        detail="the deploy script failed (exit $rc)"
    elif ! verify_stack; then
        detail="the containers or Frigate did not come up within ${VERIFY_TIMEOUT} seconds"
    fi

    if [[ -z "$detail" ]]; then
        if [[ "$ROLLED_BACK" == "true" ]]; then
            write_redeploy_result rolled_back "$from" "$to" "${REDEPLOY_DETAIL:-the update failed}"
            fatal "Rolled back to ${from:0:7} (the update to ${to:0:7} failed)"
        fi
        write_redeploy_result ok "$from" "$to" ""
        success "Auto deploy of ${to:0:7} finished"
        return 0
    fi

    error "Auto deploy failed: $detail"
    if [[ "$ROLLED_BACK" != "true" && "$from" != "$to" ]]; then
        warn "Rolling back to ${from:0:7}"
        git -C "$SOURCE_DIR" reset --quiet --hard "$from"
        REDEPLOY_FROM="$from" REDEPLOY_TO="$to" REDEPLOY_DETAIL="$detail" \
            exec "$SOURCE_DIR/deploy-edge-server.sh" --auto-apply --rolled-back
    fi
    write_redeploy_result failed "$from" "$to" "$detail"
    exit 1
}
