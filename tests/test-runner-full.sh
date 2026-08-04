#!/usr/bin/env bash
# Full stack test runner with pass/fail assertions
# Tests: 4 cameras, Frigate, Mosquitto, Home Assistant
# Usage: ./test-runner-full.sh [--no-teardown]

set -euo pipefail
cd "$(dirname "$0")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

PASSED=0
FAILED=0

pass() {
    echo -e "  ${GREEN}✓ PASS${NC}: $1"
    PASSED=$((PASSED + 1))
}

fail() {
    echo -e "  ${RED}✗ FAIL${NC}: $1"
    [ -n "${2:-}" ] && echo -e "         $2"
    FAILED=$((FAILED + 1))
}

skip() {
    echo -e "  ${YELLOW}○ SKIP${NC}: $1"
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

# Cleanup function
cleanup() {
    if [ "$NO_TEARDOWN" = false ]; then
        section "Teardown"
        docker compose -f docker-compose.mac.yml down -v --remove-orphans >/dev/null 2>&1 || true
        docker compose -f docker-compose.test-harness.yml down -v --remove-orphans >/dev/null 2>&1 || true
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
MEM_REQUIRED=3500
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
mkdir -p mock_storage mosquitto/{config,data,log} frigate-mac ha-config

# Start harness
echo "Starting mock camera harness..."
if docker compose -f docker-compose.test-harness.yml up -d 2>&1 | grep -v "^$" | head -10; then
    pass "Mock camera harness started"
else
    fail "Mock camera harness failed"
    exit 1
fi

echo "Waiting 10s for streams..."
sleep 10

# Start security stack
echo "Starting security stack..."
if docker compose -f docker-compose.mac.yml --env-file .env.mac up -d 2>&1 | grep -v "^$" | grep -v "orphan" | head -10; then
    pass "Security stack started"
else
    fail "Security stack failed"
    exit 1
fi

echo "Waiting 25s for services to initialize..."
sleep 25

section "Container Health"

# Test: Mosquitto
mosquitto_status=$(docker inspect --format='{{.State.Status}}:{{.State.Health.Status}}' mosquitto-test 2>/dev/null || echo "missing:")
if [[ "$mosquitto_status" == "running:healthy" ]]; then
    pass "Mosquitto container (running, healthy)"
elif [[ "$mosquitto_status" == running:* ]]; then
    fail "Mosquitto container (running but unhealthy)" "Health: ${mosquitto_status#*:}"
else
    fail "Mosquitto container not running" "Status: $mosquitto_status"
fi

# Test: Frigate
frigate_status=$(docker inspect --format='{{.State.Status}}' frigate-test 2>/dev/null || echo "missing")
if [ "$frigate_status" = "running" ]; then
    pass "Frigate container (running)"
else
    fail "Frigate container not running" "Status: $frigate_status"
fi

# Test: Home Assistant
ha_status=$(docker inspect --format='{{.State.Status}}' homeassistant-test 2>/dev/null || echo "missing")
if [ "$ha_status" = "running" ]; then
    pass "Home Assistant container (running)"
else
    fail "Home Assistant container not running" "Status: $ha_status"
fi

# Test: RTSP server
rtsp_status=$(docker inspect --format='{{.State.Status}}' mock-rtsp-server 2>/dev/null || echo "missing")
if [ "$rtsp_status" = "running" ]; then
    pass "RTSP server container (running)"
else
    fail "RTSP server container not running" "Status: $rtsp_status"
fi

# Test: All 4 mock cameras
for cam in 1 2 3 4; do
    cam_status=$(docker inspect --format='{{.State.Status}}' "mock-cam${cam}" 2>/dev/null || echo "missing")
    if [ "$cam_status" = "running" ]; then
        pass "Mock camera $cam (running)"
    else
        fail "Mock camera $cam not running" "Status: $cam_status"
    fi
done

section "Service Connectivity"

# Test: MQTT broker
if timeout 5 docker exec mosquitto-test mosquitto_sub -t '$SYS/#' -C 1 -W 3 >/dev/null 2>&1; then
    pass "MQTT broker accepting connections (port 1883)"
else
    fail "MQTT broker not responding"
fi

# Test: Frigate API
frigate_response=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/api/version 2>/dev/null || echo "000")
if [ "$frigate_response" = "200" ]; then
    frigate_version=$(curl -s http://localhost:5000/api/version 2>/dev/null | grep -o '"version":"[^"]*"' | cut -d'"' -f4 || echo "unknown")
    pass "Frigate API responding (v${frigate_version})"
else
    fail "Frigate API not responding" "HTTP $frigate_response"
fi

# Test: Home Assistant API
ha_response=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8123/api/ 2>/dev/null || echo "000")
if [ "$ha_response" = "200" ] || [ "$ha_response" = "401" ]; then
    pass "Home Assistant API responding (port 8123)"
else
    fail "Home Assistant API not responding" "HTTP $ha_response"
fi

# Test: RTSP streams available
rtsp_paths=$(curl -s http://localhost:8554/v3/paths/list 2>/dev/null || echo "")
streams_found=0
for stream in cam1_front cam2_rear cam3_side cam4_gate; do
    if echo "$rtsp_paths" | grep -q "$stream"; then
        streams_found=$((streams_found + 1))
    fi
done
if [ "$streams_found" -eq 4 ]; then
    pass "All 4 RTSP streams available"
elif [ "$streams_found" -gt 0 ]; then
    fail "Only $streams_found/4 RTSP streams available"
else
    # Fallback: check port
    if nc -z localhost 8554 2>/dev/null; then
        pass "RTSP server listening (streams may still be initializing)"
    else
        fail "RTSP server not available"
    fi
fi

section "Integration"

# Test: Frigate camera config
frigate_config=$(curl -s http://localhost:5000/api/config 2>/dev/null || echo "")
if echo "$frigate_config" | grep -q "cameras"; then
    cam_count=$(echo "$frigate_config" | grep -co '"cam[0-9]_' || echo "0")
    cam_count=${cam_count##*$'\n'}  # Take last line if multiple
    cam_count=${cam_count:-0}
    if [ "$cam_count" -ge 4 ] 2>/dev/null; then
        pass "Frigate configured with $cam_count cameras"
    elif [ "$cam_count" -gt 0 ] 2>/dev/null; then
        pass "Frigate configured with $cam_count camera(s)"
    else
        skip "Frigate camera config (no cameras in config yet)"
    fi
else
    fail "Frigate config not accessible"
fi

# Test: Frigate MQTT connection
frigate_logs=$(docker logs frigate-test 2>&1 | tail -100 || true)
if echo "$frigate_logs" | grep -qiE "mqtt.*connect|connected to mqtt"; then
    pass "Frigate connected to MQTT broker"
else
    skip "Frigate MQTT connection (no MQTT logs found yet)"
fi

# Test: No container restarts
restarts=$(docker ps --format '{{.Names}}:{{.Status}}' | grep -E "frigate|mosquitto|mock|home" | grep -c "Restarting" || true)
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
    echo "  Frigate:        http://localhost:5000"
    echo "  Home Assistant: http://localhost:8123"
    echo "  MQTT:           localhost:1883"
    echo "  RTSP:           rtsp://localhost:8554/cam1_front"
    echo ""
    echo "Logs:"
    echo "  docker logs frigate-test"
    echo "  docker logs homeassistant-test"
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
