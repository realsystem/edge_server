# Edge Server

[![Test](https://github.com/realsystem/edge_server/actions/workflows/test.yml/badge.svg)](https://github.com/realsystem/edge_server/actions/workflows/test.yml)

Headless Ubuntu edge server for Frigate NVR, Home Assistant, Mosquitto MQTT, and Tailscale.

## Components

| Component | Purpose | Alternatives | Cost | Complexity |
|-----------|---------|--------------|------|------------|
| **Frigate NVR** | Open-source network video recorder with real-time AI object detection (people, cars, animals). Integrates with IP cameras and sends alerts only when something meaningful happens. | Blue Iris ($70), Shinobi, ZoneMinder, cloud NVRs (Ring, Nest) | Free (benefits from Coral TPU $25-60) | Medium-high |
| **Home Assistant** | Open-source home automation hub. Unifies smart devices across vendors (Zigbee, Z-Wave, WiFi, Matter) into one dashboard with automations. | openHAB, Hubitat ($150), SmartThings, Apple HomeKit | Free ($6.50/mo optional cloud) | Medium |
| **Mosquitto MQTT** | Lightweight message broker implementing MQTT protocol. The glue between Frigate, Home Assistant, and IoT sensors via publish/subscribe topics. | EMQX, HiveMQ, RabbitMQ, AWS IoT Core | Free | Low |
| **Tailscale** | Zero-config mesh VPN built on WireGuard. Secure remote access without exposing ports to the internet. | WireGuard (manual), ZeroTier, OpenVPN, Cloudflare Tunnel | Free (personal use) | Very low |

## Quick Start (from laptop)

```bash
# Interactive deployment to target server
./bootstrap.sh 192.168.1.100

# Automated with secrets file
./bootstrap.sh --auto --secrets-file ~/.edge-secrets.env 192.168.1.100

# Dry run to see what would happen
./bootstrap.sh --dry-run 192.168.1.100

# With custom config
./bootstrap.sh --config myserver.cfg 192.168.1.100
```

Requires Python 3.10+. The wrapper script creates a venv automatically.

## Manual Setup Flow

```
Fresh Ubuntu Server → initial-setup.sh → reboot → deploy-edge-server.sh
```

## 1. Install Ubuntu Server

- Download [Ubuntu Server 24.04](https://ubuntu.com/download/server)
- Flash to USB with [balenaEtcher](https://etcher.balena.io/)
- Install with:
  - **OpenSSH server enabled**
  - Import SSH keys from GitHub (optional)
  - Skip snaps

## 2. BIOS Settings (before or after Ubuntu install)

- **Power On AC** → Always On (auto-boot after power loss)
- **Enable VT-x** (virtualization)

## 3. Run Initial Setup

```bash
ssh user@<laptop-ip>
# Copy scripts to server first, then:
chmod +x initial-setup.sh deploy-edge-server.sh
sudo ./initial-setup.sh
sudo reboot
```

This handles: system updates, static IP config, external drive formatting.

## 4. Configure Secrets

Use the encrypted secrets manager for credentials:

```bash
# Initialize secrets storage (first time only)
./secrets.sh init

# Edit secrets to add your credentials
./secrets.sh edit

# Or set individual secrets
./secrets.sh set TAILSCALE_AUTH_KEY "tskey-auth-xxxxx"
./secrets.sh set REOLINK_PASS "your-camera-password"
```

## 5. Deploy Stack

```bash
ssh user@<laptop-ip>

# Option A: Interactive (prompts for missing values)
sudo ./deploy-edge-server.sh

# Option B: With secrets loaded
eval $(./secrets.sh export)
sudo -E ./deploy-edge-server.sh

# Option C: Batch mode (no prompts, fails if secrets missing)
export BATCH_MODE=true
eval $(./secrets.sh export)
sudo -E ./deploy-security.sh
```

## 6. Post-Deployment

| Service | URL |
|---------|-----|
| Home Assistant | `http://<ip>:8123` |
| Frigate | `http://<ip>:5000` |
| MQTT | `<ip>:1883` |

Get Tailscale auth key: [admin console](https://login.tailscale.com/admin/settings/keys)

## Management

```bash
cd /opt/edge-server

# Logs
docker compose logs -f

# Restart
sudo systemctl restart edge-server

# Update
docker compose pull && docker compose up -d
```

## Files

**Orchestrator (runs on laptop):**
- `bootstrap.sh` — Python deployment orchestrator with progress display, rollback support

**Target scripts (run on Ubuntu server via SSH):**
- `initial-setup.sh` — System setup (static IP, Docker, storage)
- `deploy-edge-server.sh` — Deploy base stack (Home Assistant, MQTT, Tailscale)
- `deploy-security.sh` — Deploy security stack (Frigate, cameras)
- `secrets.sh` — Encrypted secrets management on target

**Configuration:**
- `bootstrap.cfg.example` — Configuration template
- `DEPLOYMENT.md` — Detailed deployment reference

## Development

```bash
# Create venv and install dev dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run all tests (Python + shell + Docker)
make test-scripts   # Shell script tests
pytest              # Python tests
make test           # Docker integration tests
```

## Secrets

Secrets are stored in `~/.edge-server-secrets/secrets.enc` (AES-256 encrypted).

Supported secrets:
- `TAILSCALE_AUTH_KEY` — Tailscale auth key
- `MQTT_USER` / `MQTT_PASS` — MQTT broker credentials
- `REOLINK_USER` / `REOLINK_PASS` — Camera credentials
- `EXTERNAL_DRIVE_UUID` — Storage drive UUID

```bash
./secrets.sh list      # Show stored keys
./secrets.sh get KEY   # Get a value
./secrets.sh set K V   # Set a value
./secrets.sh edit      # Edit all secrets
```
