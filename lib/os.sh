# shellcheck shell=bash
# Which Linux distribution this is, and the parts of the deploy that differ between them.
#
# os_detect reads /etc/os-release and loads lib/os/<family>.sh, which defines:
#   os_archive_url         a package mirror of the distribution (the connectivity check)
#   os_docker_repo_url     Docker's apt repository for the distribution
#   os_extra_packages      packages only this distribution needs on top of the common ones
#   os_unattended_origins  the origins block of /etc/apt/apt.conf.d/50unattended-upgrades
#
# Supported: Ubuntu and Debian. Anything else stops the deploy before it changes anything.
# To add one, write lib/os/<id>.sh with those four functions and add <id> to OS_SUPPORTED.

OS_RELEASE_FILE="${OS_RELEASE_FILE:-/etc/os-release}"  # overridable for tests
OS_SUPPORTED=(ubuntu debian)
OS_ID="" OS_FAMILY="" OS_VERSION_ID="" OS_CODENAME="" OS_PRETTY_NAME=""

# One value of os-release (ID, VERSION_CODENAME, ...), quotes removed. The file is
# read, not sourced: nothing in it runs.
os_release_value() {
    local key="$1" line
    line=$(grep -m 1 "^${key}=" "$OS_RELEASE_FILE" 2> /dev/null) || return 0
    line=${line#*=}
    line=${line#[\"\']}
    line=${line%[\"\']}
    printf '%s\n' "$line"
}

# Sets OS_ID, OS_FAMILY, OS_VERSION_ID, OS_CODENAME, OS_PRETTY_NAME and loads the
# distribution's functions. Fails (OS_FAMILY empty) on an unsupported or unknown system.
os_detect() {
    local id
    OS_FAMILY=""
    [[ -r "$OS_RELEASE_FILE" ]] || return 1
    OS_ID=$(os_release_value ID | tr '[:upper:]' '[:lower:]')
    OS_VERSION_ID=$(os_release_value VERSION_ID)
    OS_CODENAME=$(os_release_value VERSION_CODENAME)
    OS_PRETTY_NAME=$(os_release_value PRETTY_NAME)
    [[ -n "$OS_PRETTY_NAME" ]] || OS_PRETTY_NAME="${OS_ID:-unknown} ${OS_VERSION_ID}"
    for id in "${OS_SUPPORTED[@]}"; do
        if [[ "$OS_ID" == "$id" ]]; then
            OS_FAMILY="$id"
            break
        fi
    done
    [[ -n "$OS_FAMILY" && -r "$EDGE_LIB_DIR/os/$OS_FAMILY.sh" ]] || {
        OS_FAMILY=""
        return 1
    }
    # shellcheck source=lib/os/ubuntu.sh
    source "$EDGE_LIB_DIR/os/$OS_FAMILY.sh"
}

# Detect once, or stop the deploy with the reason. Safe to call from any step.
os_require() {
    [[ -n "$OS_FAMILY" ]] && return 0
    if ! os_detect; then
        fatal "Unsupported system: ${OS_PRETTY_NAME:-unknown (no $OS_RELEASE_FILE)}. This script supports Ubuntu Server and Debian."
    fi
    # Docker's apt repository is per release codename (noble, bookworm, ...)
    [[ -n "$OS_CODENAME" ]] \
        || fatal "Cannot tell the release codename of $OS_PRETTY_NAME (no VERSION_CODENAME in $OS_RELEASE_FILE)"
}
