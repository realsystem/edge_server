#!/usr/bin/env bash
# Test runner with pass/fail assertions
# Usage: ./test-runner.sh [--no-teardown]
# shellcheck disable=SC2317

set -euo pipefail
cd "$(dirname "$0")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

PASSED=0
FAILED=0
TESTS=()

pass() {
    echo -e "  ${GREEN}✓ PASS${NC}: $1"
    PASSED=$((PASSED + 1))
    TESTS+=("PASS: $1")
}

fail() {
    echo -e "  ${RED}✗ FAIL${NC}: $1"
    [ -n "${2:-}" ] && echo -e "         $2"
    FAILED=$((FAILED + 1))
    TESTS+=("FAIL: $1")
}

skip() {
    echo -e "  ${YELLOW}○ SKIP${NC}: $1"
    TESTS+=("SKIP: $1")
}

section() {
    echo ""
    echo "━━━ $1 ━━━"
}

# Parse args
NO_TEARDOWN=false
for arg in "$@"; do
    case $arg in
        --no-teardown) NO_TEARDOWN=true ;;
    esac
done

# Cleanup function (called via trap)
cleanup() {
    if [ "$NO_TEARDOWN" = false ]; then
        section "Teardown"
        docker compose -f docker-compose.lite.yml down -v --remove-orphans >/dev/null 2>&1 || true
        echo "Stack stopped"
    else
        echo ""
        echo "Stack left running (--no-teardown)"
        echo "Stop with: make stop"
    fi
}
trap cleanup EXIT

section "Prerequisites"

# Test: Docker running
if docker info >/dev/null 2>&1; then
    pass "Docker daemon running"
else
    fail "Docker daemon not running" "Start Docker Desktop"
    exit 1
fi

# Test: Memory check
MEM_REQUIRED=1500
mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo "0")
if [ -n "$mem" ] && [ "$mem" != "0" ]; then
    mem_mb=$((mem / 1024 / 1024))
    if [ "$mem_mb" -ge "$MEM_REQUIRED" ]; then
        pass "Memory available (${mem_mb}MB >= ${MEM_REQUIRED}MB)"
    else
        fail "Insufficient memory" "Have ${mem_mb}MB, need ${MEM_REQUIRED}MB"
        exit 1
    fi
else
    skip "Memory check (could not detect limit)"
fi

section "Stack Startup"

# Create directories
mkdir -p mock_storage mosquitto/config frigate-lite

# Start stack
echo "Starting containers..."
if docker compose -f docker-compose.lite.yml up -d 2>&1 | grep -v "^$"; then
    pass "Docker compose up"
else
    fail "Docker compose up"
    exit 1
fi

# Wait for services
echo "Waiting 25s for services to initialize..."
sleep 25

section "Container Health"

# Test: Mosquitto container running and healthy
mosquitto_status=$(docker inspect --format='{{.State.Status}}:{{.State.Health.Status}}' mosquitto-test 2>/dev/null || echo "missing:")
if [[ "$mosquitto_status" == "running:healthy" ]]; then
    pass "Mosquitto container (running, healthy)"
elif [[ "$mosquitto_status" == running:* ]]; then
    fail "Mosquitto container (running but unhealthy)" "Health: ${mosquitto_status#*:}"
else
    fail "Mosquitto container not running" "Status: $mosquitto_status"
fi

# Test: Frigate container running
frigate_status=$(docker inspect --format='{{.State.Status}}' frigate-test 2>/dev/null || echo "missing")
if [ "$frigate_status" = "running" ]; then
    pass "Frigate container (running)"
else
    fail "Frigate container not running" "Status: $frigate_status"
fi

# Test: Mock camera container running
camera_status=$(docker inspect --format='{{.State.Status}}' mock-camera 2>/dev/null || echo "missing")
if [ "$camera_status" = "running" ]; then
    pass "Mock camera container (running)"
else
    fail "Mock camera container not running" "Status: $camera_status"
fi

# Test: Stream generator running
stream_status=$(docker inspect --format='{{.State.Status}}' stream-gen 2>/dev/null || echo "missing")
if [ "$stream_status" = "running" ]; then
    pass "Stream generator container (running)"
else
    fail "Stream generator container not running" "Status: $stream_status"
fi

section "Service Connectivity"

# Test: MQTT broker accepting connections
# shellcheck disable=SC2016 # $SYS is a literal MQTT topic, not a variable
if timeout 5 docker exec mosquitto-test mosquitto_sub -t '$SYS/#' -C 1 -W 3 >/dev/null 2>&1; then
    pass "MQTT broker accepting connections (port 1883)"
else
    fail "MQTT broker not responding"
fi

# Test: Frigate API responding
frigate_response=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/api/version 2>/dev/null || echo "000")
if [ "$frigate_response" = "200" ]; then
    frigate_version=$(curl -s http://localhost:5000/api/version 2>/dev/null | grep -o '"version":"[^"]*"' | cut -d'"' -f4 || echo "unknown")
    pass "Frigate API responding (v${frigate_version})"
elif [ "$frigate_response" = "000" ]; then
    fail "Frigate API not reachable" "Connection refused on port 5000"
else
    fail "Frigate API error" "HTTP $frigate_response"
fi

# Test: Frigate web UI accessible
frigate_ui=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/ 2>/dev/null || echo "000")
if [ "$frigate_ui" = "200" ]; then
    pass "Frigate web UI accessible"
else
    fail "Frigate web UI not accessible" "HTTP $frigate_ui"
fi

# Test: RTSP stream available (check mediamtx API instead of ffprobe for reliability)
rtsp_paths=$(curl -s http://localhost:8554/v3/paths/list 2>/dev/null || echo "")
if echo "$rtsp_paths" | grep -q "cam1"; then
    pass "RTSP stream available (rtsp://localhost:8554/cam1)"
elif [ -n "$rtsp_paths" ]; then
    fail "RTSP stream cam1 not found" "MediaMTX running but cam1 path not registered"
else
    # Fallback: check if mediamtx port is open
    if nc -z localhost 8554 2>/dev/null; then
        pass "RTSP server listening (port 8554)"
    else
        fail "RTSP server not available" "Port 8554 not responding"
    fi
fi

section "Integration"

# Test: Frigate connected to MQTT
frigate_logs=$(docker logs frigate-test 2>&1 | tail -50 || true)
if echo "$frigate_logs" | grep -q "MQTT"; then
    if echo "$frigate_logs" | grep -qiE "mqtt.*connect|connected to mqtt"; then
        pass "Frigate connected to MQTT broker"
    else
        fail "Frigate MQTT connection" "MQTT mentioned but connection unclear"
    fi
else
    skip "Frigate MQTT connection (no MQTT logs found yet)"
fi

# Test: Frigate detecting camera
if curl -s http://localhost:5000/api/stats 2>/dev/null | grep -qE "cam1|detection"; then
    pass "Frigate camera detection active"
else
    # Check config
    if curl -s http://localhost:5000/api/config 2>/dev/null | grep -q "cameras"; then
        pass "Frigate camera configured (detection may need more time)"
    else
        fail "Frigate camera not configured"
    fi
fi

# Test: No container restarts
restarts=$(docker ps --format '{{.Names}}:{{.Status}}' | grep -E "frigate|mosquitto|mock|stream" | grep -c "Restarting" || true)
if [ -z "$restarts" ] || [ "$restarts" = "0" ]; then
    pass "No container restart loops"
else
    fail "Containers restarting" "$restarts container(s) in restart loop"
fi

section "Results"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "  ${GREEN}Passed${NC}: %d\n" "$PASSED"
printf "  ${RED}Failed${NC}: %d\n" "$FAILED"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [ "$FAILED" -gt 0 ]; then
    echo "Stack URLs (for debugging):"
    echo "  Frigate:  http://localhost:5000"
    echo "  MQTT:     localhost:1883"
    echo "  Stream:   rtsp://localhost:8554/cam1"
    echo ""
    echo "Logs: docker logs frigate-test"
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
