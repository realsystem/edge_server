#!/usr/bin/env bash
#===============================================================================
# Validate every Frigate config the deploy scripts and camera discovery can
# generate, using the real Frigate image (`frigate --validate-config`).
#
# YAML that parses can still be rejected by Frigate (e.g. `motion: mask: []`
# after Frigate changed masks to a dictionary), so CI checks against Frigate
# itself rather than only against a YAML parser.
#
# Usage: tests/validate-frigate-configs.sh [IMAGE]
#   IMAGE           default: ghcr.io/blakeblackshear/frigate:stable (what deploy uses)
#   GENERATE_ONLY=1 only write the configs (no Docker), print their paths
#===============================================================================

set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

IMAGE="${1:-ghcr.io/blakeblackshear/frigate:stable}"
WORK="${WORK_DIR:-$(mktemp -d)}"
if [[ -z "${WORK_DIR:-}" && "${GENERATE_ONLY:-0}" != "1" ]]; then
    trap 'rm -rf "$WORK"' EXIT
fi

# Run one deploy-script function with system commands stubbed out, writing
# into $WORK/<name>.
#   generate NAME SCRIPT FUNCTION [VAR=value ...]
generate() {
    local name="$1" script="$2" func="$3"
    shift 3
    mkdir -p "$WORK/$name"
    env "$@" EDGE_SERVER_DIR="$WORK/$name" EDGE_STORAGE_DIR="$WORK/storage" \
        bash -c '
            source "$1"
            log() { :; }; info() { :; }; warn() { :; }; step() { :; }; success() { :; }
            error() { echo "$*" >&2; }
            chown() { :; }
            # stand-in for `docker run ... mosquitto_passwd`: create the file
            docker() {
                local prev="" arg
                for arg in "$@"; do
                    if [[ "$prev" == "-v" && "$arg" == *:/mosquitto/config ]]; then
                        : > "${arg%%:*}/passwd"
                    fi
                    prev="$arg"
                done
            }
            "$2" > /dev/null  # only the config path below goes to stdout
        ' _ "$REPO/$script" "$func"
    echo "$WORK/$name/frigate/config.yml"
}

configs=()
configs+=("$(generate edge-default deploy-edge-server.sh create_app_stack)")
configs+=("$(generate edge-mqtt deploy-edge-server.sh create_app_stack \
    INSTALL_MQTT=true INSTALL_HOMEASSISTANT=true MQTT_USER=mqtt MQTT_PASS='s3cret#1')")

# Default config plus a camera the way discovery adds it
mkdir -p "$WORK/discovered/frigate"
cp "${configs[0]}" "$WORK/discovered/frigate/config.yml"
PYTHONPATH="$REPO/tools" python3 - "$WORK/discovered/frigate/config.yml" << 'EOF'
import sys
from pathlib import Path

import camera_discovery as cd

path = Path(sys.argv[1])
cam = cd.Camera(
    host="192.168.1.20",
    xaddr="http://192.168.1.20:8000/onvif/device_service",
    manufacturer="REOLINK",
    model="RLC-510A",
    credential=cd.Credential("admin", "p#ss {x}"),
    streams=[
        cd.Stream("000", uri="rtsp://192.168.1.20:554/Preview_01_main", width=2560, height=1920),
        cd.Stream("001", uri="rtsp://192.168.1.20:554/Preview_01_sub", width=640, height=480),
    ],
)
path.write_text(cd.insert_cameras(path.read_text(), [("reolink_20", cam)]))
EOF
configs+=("$WORK/discovered/frigate/config.yml")

if [[ "${GENERATE_ONLY:-0}" == "1" ]]; then
    printf '%s\n' "${configs[@]}"
    exit 0
fi

echo "Validating ${#configs[@]} configs with $IMAGE"
failed=0
for cfg in "${configs[@]}"; do
    name="$(basename "$(dirname "$(dirname "$cfg")")")"
    # Frigate may migrate the file in place, so validate a scratch copy
    cp "$cfg" "$WORK/check.yml"
    if out=$(docker run --rm \
        -v "$WORK/check.yml:/config/config.yml" \
        -e FRIGATE_MQTT_USER=mqtt -e FRIGATE_MQTT_PASSWORD=pw -e FRIGATE_RTSP_PASSWORD=x \
        --entrypoint python3 "$IMAGE" -u -m frigate --validate-config 2>&1); then
        echo "  ok    $name"
    else
        echo "  FAIL  $name"
        echo "$out" | tail -n 25 | sed 's/^/        /'
        if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
            # Annotation, so the reason is visible without opening the log
            msg=$(echo "$out" | tail -n 15 | sed ':a;N;$!ba;s/%/%25/g;s/\r//g;s/\n/%0A/g')
            echo "::error title=Frigate rejected ${name} config::${msg}"
        fi
        failed=$((failed + 1))
    fi
done

# The camera password must reach ffmpeg intact: Frigate URL-encodes it
# itself, so it has to be written unencoded (a pre-encoded '#' -> '%23' ended
# up as '%2523' and the camera rejected the login). Load the discovered
# config with Frigate's own code and decode the password from the ffmpeg
# command Frigate would run.
cp "$WORK/discovered/frigate/config.yml" "$WORK/check.yml"
if out=$(docker run --rm -v "$WORK/check.yml:/config/config.yml" \
    --entrypoint python3 "$IMAGE" -c '
import sys
from urllib.parse import unquote_plus
from frigate.config import FrigateConfig

expected = "p#ss {x}"
config = FrigateConfig.load(install=True)
urls = [
    arg
    for entry in config.cameras["reolink_20"].ffmpeg_cmds
    for arg in entry["cmd"]
    if isinstance(arg, str) and arg.startswith("rtsp://")
]
if not urls:
    sys.exit("no rtsp:// input found in the ffmpeg commands Frigate built")
for url in urls:
    login = url[len("rtsp://") : url.rindex("@")]
    password = unquote_plus(login.split(":", 1)[1])
    if password != expected:
        sys.exit(f"password reaches ffmpeg as {password!r}, expected {expected!r} ({url})")
print(f"camera password reaches ffmpeg intact in {len(urls)} input(s)")
' 2>&1); then
    echo "  ok    password encoding ($(echo "$out" | tail -n 1))"
else
    echo "  FAIL  password encoding"
    echo "$out" | tail -n 25 | sed 's/^/        /'
    if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
        msg=$(echo "$out" | tail -n 15 | sed ':a;N;$!ba;s/%/%25/g;s/\r//g;s/\n/%0A/g')
        echo "::error title=Camera password mangled by Frigate::${msg}"
    fi
    failed=$((failed + 1))
fi

if [[ "$failed" -gt 0 ]]; then
    echo "$failed Frigate check(s) failed"
    exit 1
fi
echo "All configs accepted by Frigate"
