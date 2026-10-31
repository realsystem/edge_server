#!/usr/bin/env bash
#===============================================================================
# Host network, firewall and service snapshot for /diag.
#
# Runs on the host as root (edge-snapshot.timer, every 10 minutes, and from
# collect-diagnostics.sh when that is run on the host). The Telegram bot's
# container cannot see any of this: it has no host network, no iptables and no
# Docker socket. It reads the file this writes instead.
#
# Usage: host-snapshot.sh [OUTPUT_FILE]    (default: print to stdout)
# Read-only: nothing here changes the system. Output is plain text; passwords
# and keys are not part of any command used.
#===============================================================================

set -uo pipefail

section() { printf '\n--- %s ---\n' "$1"; }

# run CMD [ARGS...]: print the command, then its output (or why there is none)
run() {
    printf '$ %s\n' "$*"
    if ! command -v "$1" > /dev/null 2>&1; then
        echo "(not installed: $1)"
    else
        timeout 20 "$@" 2>&1 || echo "(exit status $?)"
    fi
}

# Same, but keep only the first N lines of long output
run_head() {
    local n="$1"
    shift
    run "$@" | head -n "$n"
}

snapshot() {
    echo "Host snapshot"
    echo "Taken: $(date -Is) on $(hostname)"
    [[ $EUID -eq 0 ]] || echo "NOTE: not run as root - firewall rules and process names will be missing"

    section "Listening ports (tcp/udp, with the process that owns each)"
    run ss -tulpn
    run ss -s

    section "Firewall: UFW"
    run ufw status verbose
    run ufw status numbered

    section "Firewall: iptables filter table (what is actually enforced)"
    run iptables -S
    echo "Docker's own chain, packet counters (rules that never match show 0):"
    run iptables -L DOCKER-USER -n -v --line-numbers
    run iptables -L ufw-user-input -n -v --line-numbers

    section "Firewall: iptables NAT table (published Docker ports)"
    run iptables -t nat -S

    section "Firewall: IPv6"
    run ip6tables -S
    run sysctl net.ipv6.conf.all.disable_ipv6

    section "Firewall: nftables ruleset (first 200 lines)"
    run_head 200 nft list ruleset

    section "Docker: published ports and networks"
    run docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    run docker network ls
    run sysctl net.ipv4.ip_forward

    section "Network interfaces and routes"
    run ip -br addr
    run ip route
    run ip -6 route

    section "Tailscale"
    run_head 25 tailscale status
    run tailscale ip

    section "SSH daemon: effective settings"
    if command -v sshd > /dev/null 2>&1; then
        echo '$ sshd -T | grep (selected settings)'
        sshd -T 2>&1 | grep -Ei '^(port|listenaddress|permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|allowusers|allowgroups|maxauthtries|x11forwarding) ' || echo "(sshd -T gave nothing: needs root)"
    else
        echo "(sshd not installed)"
    fi
    run systemctl is-active ssh sshd
    run fail2ban-client status

    section "Logins"
    run last -n 10 -w
    run lastb -n 10 -w

    section "Services and timers"
    run systemctl --failed --no-pager
    echo '$ systemctl list-timers (edge-*, docker, apt)'
    systemctl list-timers --all --no-pager 2> /dev/null | grep -Ei 'NEXT|edge-|docker|apt' || echo "(none)"
    run systemctl is-active edge-server.service edge-docker-firewall.service tailscaled ufw docker

    section "Updates and reboot"
    if [[ -e /var/run/reboot-required ]]; then
        echo "REBOOT REQUIRED: $(cat /var/run/reboot-required.pkgs 2> /dev/null | tr '\n' ' ')"
    else
        echo "no reboot pending"
    fi
    run uname -r

    section "Recent system errors (journal, priority err and worse, last 30)"
    run journalctl -p err -n 30 --no-pager -q

    section "Kernel firewall drops (last 20 UFW BLOCK lines)"
    journalctl -k --no-pager -q 2> /dev/null | grep -F 'UFW BLOCK' | tail -n 20 || true
    echo "(empty if UFW logging is off or nothing was blocked)"
}

main() {
    umask 022
    if [[ $# -ge 1 ]]; then
        local out="$1" tmp
        tmp=$(mktemp "$out.XXXXXX") || exit 1
        snapshot > "$tmp" 2>&1
        chmod 644 "$tmp"
        mv "$tmp" "$out"
    else
        snapshot 2>&1
    fi
}

main "$@"
