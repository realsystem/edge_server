#!/usr/bin/env bash
# Lightweight local test - single camera, ~1.3GB RAM total

set -euo pipefail
cd "$(dirname "$0")"

case "${1:-start}" in
  start)
    echo "Creating directories..."
    mkdir -p mock_storage mosquitto/{config,data} frigate-lite

    echo "Starting lightweight stack..."
    docker compose -f docker-compose.lite.yml up -d

    echo "Waiting 20s for services..."
    sleep 20

    echo ""
    echo "=== Status ==="
    docker compose -f docker-compose.lite.yml ps
    echo ""
    echo "Frigate: http://localhost:5000"
    echo "MQTT:    localhost:1883"
    echo "Stream:  rtsp://localhost:8554/cam1"
    ;;
  stop)
    docker compose -f docker-compose.lite.yml down
    ;;
  logs)
    docker compose -f docker-compose.lite.yml logs -f "${2:-}"
    ;;
  clean)
    docker compose -f docker-compose.lite.yml down -v
    rm -rf mock_storage frigate-lite/*.db
    ;;
  *)
    echo "Usage: $0 {start|stop|logs|clean}"
    ;;
esac
