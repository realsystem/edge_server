# Local Testing Sandbox

Test the security stack on macOS before deploying to production.

## Prerequisites

- Docker Desktop or OrbStack
- ffmpeg/ffprobe (for stream validation): `brew install ffmpeg`
- jq (for JSON parsing): `brew install jq`

## Quick Start

```bash
cd testing

# Start everything (mock cameras + full stack)
./test-local-deploy.sh start

# Open in browser
open http://localhost:5000   # Frigate
open http://localhost:8123   # Home Assistant

# View logs
./test-local-deploy.sh logs frigate

# Stop everything
./test-local-deploy.sh stop

# Clean up all data
./test-local-deploy.sh clean
```

## What's Included

```
testing/
├── docker-compose.test-harness.yml   # Mock RTSP camera streams
├── docker-compose.mac.yml            # macOS-adapted stack
├── mediamtx.yml                      # RTSP server config
├── .env.mac                          # macOS environment vars
├── frigate-mac/config.yml            # CPU-only Frigate config
├── mosquitto/config/                 # MQTT broker config
├── ha-config/                        # Home Assistant config
├── mock_storage/                     # Simulated video storage
└── test-local-deploy.sh              # Test orchestration script
```

## Mock Cameras

The test harness generates 4 synthetic RTSP streams using ffmpeg test patterns:

| Camera | Main Stream (1080p) | Sub Stream (360p) |
|--------|---------------------|-------------------|
| Front | `rtsp://localhost:8554/cam1_front` | `rtsp://localhost:8554/cam1_front_sub` |
| Rear | `rtsp://localhost:8554/cam2_rear` | `rtsp://localhost:8554/cam2_rear_sub` |
| Side | `rtsp://localhost:8554/cam3_side` | `rtsp://localhost:8554/cam3_side_sub` |
| Gate | `rtsp://localhost:8554/cam4_gate` | `rtsp://localhost:8554/cam4_gate_sub` |

Test with VLC:
```bash
vlc rtsp://localhost:8554/cam1_front
```

## macOS Adaptations

| Production | macOS Test |
|------------|------------|
| VA-API GPU acceleration | CPU-only decoding |
| `network_mode: host` | Explicit port mappings |
| `/mnt/storage/frigate` | `./mock_storage` |
| Real camera IPs | `host.docker.internal:8554` |
| `/dev/dri/renderD128` | Not mounted |

## Commands

```bash
./test-local-deploy.sh start      # Start mock cameras + stack
./test-local-deploy.sh stop       # Stop all containers
./test-local-deploy.sh status     # Show container status
./test-local-deploy.sh logs       # Follow all logs
./test-local-deploy.sh logs frigate  # Follow specific service
./test-local-deploy.sh validate   # Check RTSP streams
./test-local-deploy.sh health     # Run health checks
./test-local-deploy.sh clean      # Stop + remove data
```

## Troubleshooting

### Frigate shows cameras as offline

1. Check mock streams are running:
   ```bash
   docker logs mock-cam1
   ```

2. Verify RTSP server:
   ```bash
   curl http://localhost:8889/v3/paths/list | jq .
   ```

3. Test stream directly:
   ```bash
   ffplay -rtsp_transport tcp rtsp://localhost:8554/cam1_front
   ```

### Port conflicts

If ports are in use:
```bash
lsof -i :5000  # Find process using port
```

### Container crashes

Check logs:
```bash
docker logs frigate-test
docker logs mosquitto-test
```

### Cleanup stuck containers

```bash
docker compose -f docker-compose.test-harness.yml down -v
docker compose -f docker-compose.mac.yml down -v
docker network rm edge-server-test
```
