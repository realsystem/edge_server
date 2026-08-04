# Edge Server Deployment

Automated provisioning for an Ubuntu-based edge server running Frigate NVR, Home Assistant, Mosquitto MQTT, and Tailscale.

## Prerequisites

- Fresh Ubuntu Server 22.04+ installation
- Minimum 4GB RAM (8GB recommended for Frigate)
- External USB drive for video storage (recommended)
- Network connectivity
- SSH access

## Quick Start

```bash
# 1. Copy deployment files to the server
scp deploy-edge-server.sh env.example user@server:~/

# 2. SSH into the server
ssh user@server

# 3. Optional: Set environment variables
cp env.example .env
# Edit .env with your values
source .env

# 4. Run the deployment
sudo -E ./deploy-edge-server.sh
```

## Configuration Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TAILSCALE_AUTH_KEY` | Reusable auth key from Tailscale admin | (prompted) |
| `EXTERNAL_DRIVE_UUID` | UUID of external storage drive | (prompted) |
| `LOCAL_SUBNET` | Allowed subnet for firewall | `192.168.0.0/16` |
| `TIMEZONE` | Server timezone | `America/Los_Angeles` |
| `MQTT_USER` | MQTT broker username | `admin` |
| `MQTT_PASS` | MQTT broker password | (auto-generated) |

## Finding External Drive UUID

```bash
# List all block devices with UUIDs
lsblk -o NAME,UUID,SIZE,FSTYPE,MOUNTPOINT

# Or use blkid
sudo blkid
```

## Post-Installation

### Add Cameras to Frigate

Edit `/opt/edge-server/frigate/config.yml`:

```yaml
cameras:
  front_cam:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://admin:password@192.168.1.100:554/stream
          roles:
            - detect
            - record
    detect:
      width: 1920
      height: 1080
      fps: 5
    objects:
      track:
        - person
        - car
        - dog
        - cat
```

Then restart Frigate:
```bash
cd /opt/edge-server && docker compose restart frigate
```

### Complete Home Assistant Setup

1. Open `http://<server-ip>:8123`
2. Create your admin account
3. Add MQTT integration (Settings → Devices → Add Integration → MQTT)
4. Add Frigate integration via HACS or manual config

### Remote Access via Tailscale

After authentication, access services via Tailscale IP:
```bash
# Check Tailscale status and IP
tailscale status
tailscale ip -4
```

## Management Commands

```bash
# View all container logs
cd /opt/edge-server && docker compose logs -f

# View specific service logs
docker logs -f frigate
docker logs -f homeassistant
docker logs -f mosquitto

# Restart the entire stack
sudo systemctl restart edge-server

# Check service status
sudo systemctl status edge-server
cd /opt/edge-server && docker compose ps

# Update all containers
cd /opt/edge-server
docker compose pull
docker compose up -d

# Check disk usage
df -h /mnt/storage
du -sh /mnt/storage/frigate/*
```

## Troubleshooting

### Containers Not Starting

```bash
# Check Docker status
sudo systemctl status docker

# Check container logs
docker logs mosquitto
docker logs homeassistant
docker logs frigate

# Rebuild containers
cd /opt/edge-server
docker compose down
docker compose up -d
```

### External Drive Not Mounting

```bash
# Check if drive is detected
lsblk
sudo blkid

# Try manual mount
sudo mount /mnt/storage

# Check fstab syntax
cat /etc/fstab
sudo mount -a
```

### Firewall Issues

```bash
# Check UFW status
sudo ufw status verbose

# Temporarily disable for testing
sudo ufw disable

# Re-enable after testing
sudo ufw enable
```

### Intel QuickSync Not Working

```bash
# Check if render device exists
ls -la /dev/dri/

# Check vaapi support
docker exec frigate vainfo
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Ubuntu Server                           │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Tailscale  │  │     UFW     │  │ unattended-upgrades │  │
│  │  (VPN mesh) │  │  (firewall) │  │  (security patches) │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│                    Docker Engine                            │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                  edge-server network                     │  │
│  │  ┌──────────┐  ┌──────────────┐  ┌──────────────────┐ │  │
│  │  │Mosquitto │  │Home Assistant│  │     Frigate      │ │  │
│  │  │  :1883   │◄─┤    :8123     │  │      :5000       │ │  │
│  │  │  (MQTT)  │  │ (host mode)  │  │  (QuickSync HW)  │ │  │
│  │  └──────────┘  └──────────────┘  └──────────────────┘ │  │
│  └───────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  /opt/edge-server         │  /mnt/storage                     │
│  (config + compose)     │  (video recordings)               │
└─────────────────────────┴───────────────────────────────────┘
```

## Files Reference

| Path | Description |
|------|-------------|
| `/opt/edge-server/docker-compose.yml` | Main compose file |
| `/opt/edge-server/frigate/config.yml` | Frigate NVR configuration |
| `/opt/edge-server/ha-config/` | Home Assistant configuration |
| `/opt/edge-server/mosquitto/config/` | Mosquitto broker config |
| `/mnt/storage/frigate/` | Video recordings and clips |
| `/var/log/edge-server-deploy.log` | Deployment log |
| `/var/log/edge-server-health.log` | Health check log |
| `/etc/systemd/system/edge-server.service` | Auto-start service |
