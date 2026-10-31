#!/usr/bin/env bash
#===============================================================================
# Collect hardware, load and Frigate information for performance tuning.
#
# Usage:
#   sudo ./collect-diagnostics.sh [OUTPUT_FILE]
#
# Writes a text report (default ./edge-diagnostics-<time>.txt, mode 600) and
# prints its path. Run it while the cameras are recording, ideally once with
# nobody watching live view. Needs no network access and changes nothing.
#
# Passwords are removed: camera passwords from .cam, "user:pass@" in stream
# URLs and password/token/secret settings. Read the report before sharing it;
# it does include model names, private camera IPs and the Frigate config.
#
# Environment:
#   EDGE_SERVER_DIR  default /opt/edge-server
#   FRIGATE_URL      default http://127.0.0.1:5000 (unauthenticated API port)
#   SAMPLES, INTERVAL  load samples to take and seconds between them (3, 5)
#===============================================================================

set -uo pipefail

APP_DIR="${EDGE_SERVER_DIR:-/opt/edge-server}"
FRIGATE_URL="${FRIGATE_URL:-http://127.0.0.1:5000}"
FRIGATE_CONTAINER="${FRIGATE_CONTAINER:-frigate}"
SAMPLES="${SAMPLES:-3}"
INTERVAL="${INTERVAL:-5}"
OUT="${1:-./edge-diagnostics-$(date +%Y%m%d-%H%M%S).txt}"

section() { printf '\n===== %s =====\n' "$1"; }

# run CMD [ARGS...]: print the command, then its output (or why there is none)
run() {
    printf '$ %s\n' "$*"
    if ! command -v "$1" > /dev/null 2>&1; then
        echo "(not installed: $1)"
    else
        timeout 30 "$@" 2>&1 || echo "(exit status $?)"
    fi
}

# Everything below goes through redact() before it reaches the report.
redact() {
    sed -E \
        -e 's#(://)[^[:space:]"]*@#\1***@#g' \
        -e 's/((pass(word)?|passwd|secret|token|api_?key)[A-Za-z0-9_]*[ ]*[=:][ ]*"?)[^ ",]+/\1***/Ig'
}

# Replace the literal passwords from .cam, whatever form they appear in.
scrub_cam_passwords() {
    local file="$1" cam="$APP_DIR/.cam"
    [[ -r "$cam" ]] || return 0
    command -v python3 > /dev/null 2>&1 || return 0
    python3 - "$file" "$cam" << 'PY'
import ipaddress
import sys
import urllib.parse

report, cam = sys.argv[1:3]
passwords = set()
for raw in open(cam, errors="replace").read().splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    first, _, rest = line.partition(" ")
    try:
        ipaddress.ip_address(first)
        line = rest.strip()
    except ValueError:
        pass
    password = line.partition(":")[2]
    if len(password) >= 4:
        passwords.update(
            {
                password,
                urllib.parse.quote(password, safe=""),
                password.replace("{", "{{").replace("}", "}}"),  # as written to Frigate configs
            }
        )
text = open(report, errors="replace").read()
for password in sorted(passwords, key=len, reverse=True):
    text = text.replace(password, "***")
open(report, "w").write(text)
PY
}

report() {
    echo "Edge server diagnostics"
    echo "Collected: $(date -Is)"
    [[ $EUID -eq 0 ]] || echo "NOTE: not run as root - some files and docker output may be missing"

    section "System"
    run uptime
    run uname -srm
    grep PRETTY_NAME /etc/os-release 2> /dev/null
    run systemd-detect-virt
    run free -h
    run lsblk -o NAME,SIZE,TYPE,ROTA,FSTYPE,MOUNTPOINT
    run df -h / "$APP_DIR"

    section "Laptop / board"
    for f in sys_vendor product_name product_version bios_version bios_date; do
        printf '%s: %s\n' "$f" "$(cat "/sys/class/dmi/id/$f" 2> /dev/null || echo '?')"
    done

    section "CPU"
    lscpu 2> /dev/null | grep -E '^(Model name|Architecture|CPU\(s\)|Thread|Core|Socket|CPU max MHz|CPU min MHz|L2|L3)' ||
        grep -m1 'model name' /proc/cpuinfo
    echo "features: $(grep -m1 '^flags' /proc/cpuinfo 2> /dev/null |
        grep -owE 'avx512[a-z_]*|avx2|avx|sse4_2|vmx|aes' | sort -u | tr '\n' ' ')"
    echo "governors:"
    sort /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2> /dev/null | uniq -c
    for f in /sys/devices/system/cpu/intel_pstate/status /sys/devices/system/cpu/intel_pstate/no_turbo; do
        [[ -r "$f" ]] && printf '%s: %s\n' "$(basename "$f")" "$(cat "$f")"
    done
    echo "current / max frequency (kHz), first 8 CPUs:"
    for c in /sys/devices/system/cpu/cpu[0-7]; do
        [[ -r "$c/cpufreq/scaling_cur_freq" ]] &&
            printf '  %s %s / %s\n' "$(basename "$c")" "$(cat "$c/cpufreq/scaling_cur_freq")" \
                "$(cat "$c/cpufreq/cpuinfo_max_freq" 2> /dev/null || echo '?')"
    done
    echo "thermal throttle events per core (non-zero = throttled):"
    grep -H . /sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count 2> /dev/null |
        head -8 || true
    echo "temperatures:"
    for z in /sys/class/thermal/thermal_zone*; do
        [[ -r "$z/temp" ]] && printf '  %s: %s C\n' "$(cat "$z/type" 2> /dev/null)" "$(($(cat "$z/temp") / 1000))"
    done
    echo "power:"
    for p in /sys/class/power_supply/*; do
        [[ -d "$p" ]] || continue
        printf '  %s: type=%s status=%s online=%s capacity=%s\n' "$(basename "$p")" \
            "$(cat "$p/type" 2> /dev/null)" "$(cat "$p/status" 2> /dev/null)" \
            "$(cat "$p/online" 2> /dev/null)" "$(cat "$p/capacity" 2> /dev/null)"
    done

    section "GPU and accelerators"
    if command -v lspci > /dev/null 2>&1; then
        lspci -nn 2> /dev/null | grep -Ei 'vga|3d|display|processing accel|neural|npu|coral|co-processor'
    else
        echo "(lspci not installed: sudo apt install pciutils)"
    fi
    run ls -l /dev/dri
    run lsusb
    echo "kernel gpu drivers: $(lsmod 2> /dev/null | grep -oE '^(i915|xe|amdgpu|nouveau|nvidia)' | sort -u | tr '\n' ' ')"
    run vainfo

    section "Load: top, $SAMPLES samples every ${INTERVAL}s (first, since-boot sample skipped)"
    if command -v top > /dev/null 2>&1; then
        top -b -w 200 -o %CPU -d "$INTERVAL" -n "$((SAMPLES + 1))" 2> /dev/null |
            awk '/^top - /{n++} n>1 && ++l[n]<=17 {print}'
    fi

    section "Busiest processes (full command lines: shows what ffmpeg is doing)"
    run ps -eo pid,pcpu,pmem,etimes,nlwp,args --sort=-pcpu -ww | head -n 31

    section "Docker"
    run docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
    run docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}'
    run docker inspect "$FRIGATE_CONTAINER" --format \
        'image={{.Config.Image}} devices={{.HostConfig.Devices}} shm={{.HostConfig.ShmSize}} privileged={{.HostConfig.Privileged}} restarts={{.RestartCount}}'
    run docker exec "$FRIGATE_CONTAINER" ls /openvino-model /dev/dri
    echo "recent Frigate warnings/errors:"
    docker logs --tail 400 "$FRIGATE_CONTAINER" 2>&1 | grep -Ei 'error|warn|fail|exception' | tail -n 25

    section "Frigate API"
    if command -v curl > /dev/null 2>&1; then
        echo "version: $(curl -s -m 5 "$FRIGATE_URL/api/version" || echo 'unreachable')"
        stats=$(curl -s -m 10 "$FRIGATE_URL/api/stats" || true)
        if [[ -n "$stats" ]] && command -v python3 > /dev/null 2>&1; then
            STATS="$stats" python3 - << 'PY' 2>&1 || echo "(could not read stats)"
import json
import os

s = json.loads(os.environ["STATS"])
print("detectors (inference_speed is ms per frame):")
for name, d in s.get("detectors", {}).items():
    print(f"  {name}: inference_speed={d.get('inference_speed')} pid={d.get('pid')}")
print("cameras (skipped_fps = frames dropped because the detector was busy):")
for name, c in s.get("cameras", {}).items():
    print(
        f"  {name}: camera_fps={c.get('camera_fps')} process_fps={c.get('process_fps')} "
        f"detection_fps={c.get('detection_fps')} skipped_fps={c.get('skipped_fps')}"
    )
print("cpu_usages (percent):")
usages = s.get("cpu_usages", {})
for pid, u in sorted(usages.items(), key=lambda kv: -float(kv[1].get("cpu") or 0))[:15]:
    print(f"  pid {pid}: cpu={u.get('cpu')} mem={u.get('mem')} {str(u.get('cmdline'))[:120]}")
print("gpu_usages:", json.dumps(s.get("gpu_usages", {})))
PY
        else
            echo "stats: unavailable"
        fi
    else
        echo "(curl not installed)"
    fi

    section "Frigate config (comments removed)"
    if [[ -r "$APP_DIR/frigate/config.yml" ]]; then
        grep -vE '^[[:space:]]*(#|$)' "$APP_DIR/frigate/config.yml"
    else
        echo "(cannot read $APP_DIR/frigate/config.yml - run with sudo?)"
    fi

    section "docker-compose.yml (comments removed)"
    if [[ -r "$APP_DIR/docker-compose.yml" ]]; then
        grep -vE '^[[:space:]]*(#|$)' "$APP_DIR/docker-compose.yml"
    else
        echo "(cannot read $APP_DIR/docker-compose.yml)"
    fi
}

main() {
    umask 077
    echo "Collecting (about $((SAMPLES * INTERVAL + 10)) seconds)..." >&2
    report 2>&1 | redact > "$OUT"
    scrub_cam_passwords "$OUT"
    chmod 600 "$OUT"
    echo "Report written to $OUT" >&2
    echo "Passwords are removed, but read it before sharing: it lists private IPs and your config." >&2
}

main "$@"
