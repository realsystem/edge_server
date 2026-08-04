#!/usr/bin/env bash
#===============================================================================
# Local Testing Script for Security Stack
# Runs mock cameras and full stack on macOS for validation
#
# Usage: ./test-local-deploy.sh [start|stop|status|logs|clean]
#===============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

#-------------------------------------------------------------------------------
# Configuration
#-------------------------------------------------------------------------------

readonly HARNESS_COMPOSE="docker-compose.test-harness.yml"
readonly STACK_COMPOSE="docker-compose.mac.yml"
readonly NETWORK_NAME="edge-server-test"

readonly STREAMS=(
    "cam1_front"
    "cam1_front_sub"
    "cam2_rear"
    "cam2_rear_sub"
    "cam3_side"
    "cam3_side_sub"
    "cam4_gate"
    "cam4_gate_sub"
)

#-------------------------------------------------------------------------------
# Colors
#-------------------------------------------------------------------------------

header()  { echo ""; echo "==============================================================="; echo "  $*"; echo "==============================================================="; }
info()    { echo "[OK] $*"; }
warn()    { echo "[WARN] $*"; }
error()   { echo "[ERROR] $*"; }
step()    { echo "[..] $*"; }

#-------------------------------------------------------------------------------
# Helper Functions
#-------------------------------------------------------------------------------

check_docker() {
    if ! command -v docker &> /dev/null; then
        error "Docker not found. Install Docker Desktop or OrbStack."
        exit 1
    fi

    if ! docker info &> /dev/null; then
        error "Docker daemon not running. Start Docker Desktop or OrbStack."
        exit 1
    fi
}

create_directories() {
    mkdir -p mock_storage/{recordings,clips}
    mkdir -p mosquitto/{config,data,log}
    mkdir -p frigate-mac
    mkdir -p ha-config
    chmod -R 777 mosquitto  # Mosquitto runs as different user
}

create_network() {
    # Network is created by docker compose, no manual creation needed
    docker network rm "$NETWORK_NAME" 2>/dev/null || true
}

wait_for_port() {
    local host="$1"
    local port="$2"
    local timeout="${3:-30}"
    local elapsed=0

    while ! nc -z "$host" "$port" 2>/dev/null; do
        sleep 1
        elapsed=$((elapsed + 1))
        if [[ $elapsed -ge $timeout ]]; then
            return 1
        fi
    done
    return 0
}

#-------------------------------------------------------------------------------
# Start Mock Camera Harness
#-------------------------------------------------------------------------------

start_harness() {
    header "Starting Mock Camera Harness"

    create_network

    step "Launching RTSP server and mock camera streams..."
    docker compose -f "$HARNESS_COMPOSE" up -d

    step "Waiting for RTSP server (port 8554)..."
    if wait_for_port localhost 8554 30; then
        info "RTSP server ready"
    else
        error "RTSP server failed to start"
        return 1
    fi

    # Wait for streams to initialize
    step "Waiting for mock streams to initialize (15s)..."
    sleep 15
}

#-------------------------------------------------------------------------------
# Validate RTSP Streams
#-------------------------------------------------------------------------------

validate_streams() {
    header "Validating RTSP Streams"

    local all_ok=true

    printf "\n%-25s %-40s %-10s\n" "STREAM" "URL" "STATUS"
    printf "%-25s %-40s %-10s\n" "-------------------------" "----------------------------------------" "----------"

    for stream in "${STREAMS[@]}"; do
        local url="rtsp://localhost:8554/${stream}"
        local status

        if timeout 5 ffprobe -v quiet -rtsp_transport tcp "$url" 2>/dev/null; then
            status="PASS"
        else
            if curl -s "http://localhost:8889/v3/paths/list" 2>/dev/null | grep -q "\"$stream\""; then
                status="WAIT"
            else
                status="FAIL"
                all_ok=false
            fi
        fi

        printf "%-25s %-40s %-10s\n" "$stream" "$url" "$status"
    done

    echo
    if [[ "$all_ok" == true ]]; then
        info "All streams validated"
    else
        warn "Some streams may still be initializing"
    fi
}

#-------------------------------------------------------------------------------
# Start Main Stack
#-------------------------------------------------------------------------------

start_stack() {
    header "Starting Security Stack"

    create_directories

    step "Launching Mosquitto, Frigate, and Home Assistant..."
    docker compose -f "$STACK_COMPOSE" --env-file .env.mac up -d

    step "Waiting for services to initialize..."
    sleep 10
}

#-------------------------------------------------------------------------------
# Health Checks
#-------------------------------------------------------------------------------

run_health_checks() {
    header "Service Health Checks"

    local all_ok=true

    printf "\n%-20s %-30s %-12s %-10s\n" "SERVICE" "ENDPOINT" "PORT" "STATUS"
    printf "%-20s %-30s %-12s %-10s\n" "--------------------" "------------------------------" "------------" "----------"

    # MQTT
    if wait_for_port localhost 1883 5; then
        printf "%-20s %-30s %-12s %-10s\n" "Mosquitto MQTT" "localhost:1883" "1883" "PASS"
    else
        printf "%-20s %-30s %-12s %-10s\n" "Mosquitto MQTT" "localhost:1883" "1883" "FAIL"
        all_ok=false
    fi

    # Frigate
    if curl -sf http://localhost:5000/api/version &>/dev/null; then
        local version
        version=$(curl -s http://localhost:5000/api/version | tr -d '"')
        printf "%-20s %-30s %-12s %-10s\n" "Frigate NVR" "http://localhost:5000" "5000" "PASS"
        info "Frigate version: $version"
    else
        printf "%-20s %-30s %-12s %-10s\n" "Frigate NVR" "http://localhost:5000" "5000" "FAIL"
        all_ok=false
    fi

    # Home Assistant
    if curl -sf http://localhost:8123 &>/dev/null; then
        printf "%-20s %-30s %-12s %-10s\n" "Home Assistant" "http://localhost:8123" "8123" "PASS"
    else
        printf "%-20s %-30s %-12s %-10s\n" "Home Assistant" "http://localhost:8123" "8123" "INIT"
        warn "Home Assistant may still be initializing (this is normal)"
    fi

    # RTSP Server (harness)
    if wait_for_port localhost 8554 2; then
        printf "%-20s %-30s %-12s %-10s\n" "RTSP Server" "rtsp://localhost:8554" "8554" "PASS"
    else
        printf "%-20s %-30s %-12s %-10s\n" "RTSP Server" "rtsp://localhost:8554" "8554" "FAIL"
        all_ok=false
    fi

    echo
    return $([[ "$all_ok" == true ]] && echo 0 || echo 1)
}

#-------------------------------------------------------------------------------
# Check Camera Status in Frigate
#-------------------------------------------------------------------------------

check_frigate_cameras() {
    header "Frigate Camera Status"

    if ! curl -sf http://localhost:5000/api/stats &>/dev/null; then
        warn "Frigate API not ready yet"
        return 1
    fi

    local cameras
    cameras=$(curl -s http://localhost:5000/api/stats | jq -r '.cameras | keys[]' 2>/dev/null)

    if [[ -z "$cameras" ]]; then
        warn "No cameras configured in Frigate"
        return 1
    fi

    printf "\n%-20s %-15s %-15s %-10s\n" "CAMERA" "DETECTION" "RECORDING" "FPS"
    printf "%-20s %-15s %-15s %-10s\n" "--------------------" "---------------" "---------------" "----------"

    for cam in $cameras; do
        local detection recording fps
        detection=$(curl -s http://localhost:5000/api/stats | jq -r ".cameras.\"$cam\".detection_enabled // false")
        recording=$(curl -s http://localhost:5000/api/stats | jq -r ".cameras.\"$cam\".recording_enabled // false")
        fps=$(curl -s http://localhost:5000/api/stats | jq -r ".cameras.\"$cam\".camera_fps // 0")

        local det_status rec_status
        [[ "$detection" == "true" ]] && det_status="ON" || det_status="OFF"
        [[ "$recording" == "true" ]] && rec_status="ON" || rec_status="OFF"

        printf "%-20s %-15s %-15s %-10s\n" "$cam" "$det_status" "$rec_status" "$fps"
    done
    echo
}

#-------------------------------------------------------------------------------
# Show Status
#-------------------------------------------------------------------------------

show_status() {
    header "Container Status"

    echo ""
    echo "Mock Camera Harness:"
    docker compose -f "$HARNESS_COMPOSE" ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "Not running"

    echo ""
    echo "Security Stack:"
    docker compose -f "$STACK_COMPOSE" ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "Not running"
}

#-------------------------------------------------------------------------------
# Show Logs
#-------------------------------------------------------------------------------

show_logs() {
    local service="${1:-}"

    if [[ -n "$service" ]]; then
        docker logs -f "$service-test" 2>/dev/null || docker logs -f "$service" 2>/dev/null
    else
        header "Following All Logs (Ctrl+C to exit)"
        docker compose -f "$STACK_COMPOSE" logs -f
    fi
}

#-------------------------------------------------------------------------------
# Stop Everything
#-------------------------------------------------------------------------------

stop_all() {
    header "Stopping Test Environment"

    step "Stopping security stack..."
    docker compose -f "$STACK_COMPOSE" down 2>/dev/null || true

    step "Stopping mock camera harness..."
    docker compose -f "$HARNESS_COMPOSE" down 2>/dev/null || true

    info "All containers stopped"
}

#-------------------------------------------------------------------------------
# Clean Up
#-------------------------------------------------------------------------------

clean_all() {
    header "Cleaning Test Environment"

    stop_all

    step "Removing Docker network..."
    docker network rm "$NETWORK_NAME" 2>/dev/null || true

    step "Removing test data..."
    rm -rf mock_storage mosquitto/data mosquitto/log frigate-mac/*.db frigate-mac/frigate.db-* 2>/dev/null || true

    info "Cleanup complete"
}

#-------------------------------------------------------------------------------
# Print Summary
#-------------------------------------------------------------------------------

print_summary() {
    header "Test Environment Ready"

    echo ""
    echo "Access URLs:"
    echo "  - Frigate NVR:      http://localhost:5000"
    echo "  - Home Assistant:   http://localhost:8123"
    echo "  - MQTT Broker:      localhost:1883"
    echo

    echo "RTSP Streams (for VLC/ffplay):"
    echo "  - rtsp://localhost:8554/cam1_front"
    echo "  - rtsp://localhost:8554/cam2_rear"
    echo "  - rtsp://localhost:8554/cam3_side"
    echo "  - rtsp://localhost:8554/cam4_gate"
    echo

    echo "Commands:"
    echo "  - View status:      ./test-local-deploy.sh status"
    echo "  - View logs:        ./test-local-deploy.sh logs [frigate|mosquitto|homeassistant]"
    echo "  - Stop all:         ./test-local-deploy.sh stop"
    echo "  - Clean up:         ./test-local-deploy.sh clean"
    echo

    echo "Test Stream with VLC:"
    echo "  vlc rtsp://localhost:8554/cam1_front"
    echo
}

#-------------------------------------------------------------------------------
# Main
#-------------------------------------------------------------------------------

main() {
    local cmd="${1:-start}"

    check_docker

    case "$cmd" in
        start)
            start_harness
            validate_streams
            start_stack
            sleep 5
            run_health_checks
            check_frigate_cameras
            print_summary
            ;;
        stop)
            stop_all
            ;;
        status)
            show_status
            run_health_checks
            check_frigate_cameras
            ;;
        logs)
            show_logs "${2:-}"
            ;;
        clean)
            clean_all
            ;;
        validate)
            validate_streams
            ;;
        health)
            run_health_checks
            check_frigate_cameras
            ;;
        *)
            echo "Usage: $0 {start|stop|status|logs|clean|validate|health}"
            echo
            echo "Commands:"
            echo "  start     - Start mock cameras and full stack"
            echo "  stop      - Stop all containers"
            echo "  status    - Show container status and health"
            echo "  logs      - Follow container logs (optional: service name)"
            echo "  clean     - Stop and remove all test data"
            echo "  validate  - Validate RTSP streams only"
            echo "  health    - Run health checks only"
            exit 1
            ;;
    esac
}

main "$@"
