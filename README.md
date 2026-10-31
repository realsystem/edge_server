# Edge Server

[![Test](https://github.com/realsystem/edge_server/actions/workflows/test.yml/badge.svg)](https://github.com/realsystem/edge_server/actions/workflows/test.yml)

Headless Ubuntu edge server for Frigate NVR and Tailscale, with optional Home Assistant and
Mosquitto MQTT.

## Components

| Component | Purpose | Alternatives | Cost | Complexity |
|-----------|---------|--------------|------|------------|
| **Frigate NVR** | Open-source network video recorder with real-time AI object detection (people, cars, animals). Integrates with IP cameras and sends alerts only when something meaningful happens. | Blue Iris ($70), Shinobi, ZoneMinder, cloud NVRs (Ring, Nest) | Free (benefits from Coral TPU $25-60) | Medium-high |
| **Home Assistant** *(optional, off by default)* | Open-source home automation hub. Unifies smart devices across vendors (Zigbee, Z-Wave, WiFi, Matter) into one dashboard with automations. | openHAB, Hubitat ($150), SmartThings, Apple HomeKit | Free ($6.50/mo optional cloud) | Medium |
| **Mosquitto MQTT** *(optional, off by default)* | Lightweight message broker implementing MQTT protocol. The glue between Frigate, Home Assistant, and IoT sensors via publish/subscribe topics. | EMQX, HiveMQ, RabbitMQ, AWS IoT Core | Free | Low |
| **Tailscale** | Zero-config mesh VPN built on WireGuard. Secure remote access without exposing ports to the internet. | WireGuard (manual), ZeroTier, OpenVPN, Cloudflare Tunnel | Free (personal use) | Very low |

## Plugins

Optional monitoring plugins. They publish to Home Assistant via MQTT when
`INSTALL_MQTT=true`; otherwise their readings are only logged.

| Plugin | Purpose | Requirements |
|--------|---------|--------------|
| **Victron Smart Shunt** | Battery monitor via BLE. Reads voltage, current, SoC from Victron SmartShunt. | Victron SmartShunt with Bluetooth, encryption key from Victron Connect app |
| **Renogy Rover MPPT** | Solar charger monitor via BLE. Reads PV voltage/power, battery status, charge state. | Renogy MPPT controller with BT-2 module |
| **NUT Power Monitor** | Auto-shutdown on power loss. Monitors laptop battery and triggers shutdown after configurable time on battery. | Laptop with battery (Linux) |

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
eval "$(./secrets.sh export)"
sudo -E ./deploy-edge-server.sh

# Option C: Batch mode (no prompts)
export BATCH_MODE=true
eval "$(./secrets.sh export)"
sudo -E ./deploy-edge-server.sh
```

By default this deploys **Frigate only**. Add Home Assistant and/or the MQTT
broker with `INSTALL_HOMEASSISTANT=true` / `INSTALL_MQTT=true` (environment, or
`[deploy]` in `bootstrap.cfg`). Turning them off on an existing server removes
their containers but keeps `ha-config/` and `mosquitto/`, so they can be
switched back on later.

### Cameras (automatic discovery)

ONVIF cameras on the same network are found and added to Frigate
automatically. Put the camera logins in a `.cam` file next to the scripts, one
login per line, **keyed by the camera's IP address**:

```bash
cat > .cam << 'EOF'
192.168.1.20 admin:password-of-this-camera
192.168.1.21 admin:password-of-another-camera
# optional fallback for cameras without their own line (only the first is used)
admin:common-password
EOF
chmod 600 .cam
```

Each camera is logged in to **once**, with its own line (or the first fallback
line). A wrong password is never retried with other logins, because many cameras
lock the account after a few failed attempts. A camera with no login at all is
listed but never sent one. Reserve the cameras' addresses in your router (DHCP
reservation) so the IP keys stay valid. `--try-all-logins` restores trying every
fallback line, at the risk of locking cameras.

The deploy script then adds every new camera it finds, recording from the
highest-resolution stream and detecting on the lowest. Cameras already in the
Frigate config (matched by IP) are left alone, and the config is backed up
before any change. `.cam` is git-ignored; a root-only copy is kept at
`/opt/edge-server/.cam`.

To look for cameras again later (e.g. after adding one):

```bash
sudo ./discover-cameras.sh --dry-run          # show what would be added
sudo ./discover-cameras.sh                    # add them and restart Frigate
sudo ./discover-cameras.sh --host 192.168.1.50   # camera on another subnet
sudo ./discover-cameras.sh --verbose --dry-run   # trace every step (debugging)
```

Cameras need ONVIF turned on (Reolink: *Settings > Network > Advanced > Server
settings*). Discovery only reaches cameras on the server's own network
segment; use `--host` for others.

If a camera is found but rejects the login, `--verbose` prints a timestamped
trace to stderr (also `CAM_DEBUG=1`): which logins were loaded, who answered
discovery, each ONVIF call with its HTTP status and the camera's fault text,
and the camera's clock. Passwords are never printed, only their length.

### Telegram bot (`/status`, `/diag`)

A small bot that runs next to Frigate and answers you in a Telegram chat:

| Command | Answer |
|---|---|
| `/status` | One screen: load, CPU, memory, disk, temperature, AC/battery, Frigate detector speed and every camera's FPS, with a list of problems on top |
| `/diag` | The full diagnostics report from `collect-diagnostics.sh`, sent as a file (about 15 s) |
| `/testalert [percent]` | Sends you a simulated low-disk alert (default 5% free) plus the real disk state, so you can check delivery without filling a disk |
| `/reboot <code>` | Reboots the laptop; needs a code from your authenticator app (off until set up, see below) |
| `/id` | Your Telegram user id (works for anyone; used for setup) |

Setup:

1. In Telegram, talk to **@BotFather**, send `/newbot` and copy the token it gives you.
2. On the server, create `.telegram` next to the scripts (this is not committed to git):
   ```bash
   cat > .telegram << 'EOF'
   TELEGRAM_BOT_TOKEN=123456789:your-token-from-botfather
   TELEGRAM_ALLOWED_IDS=
   EOF
   chmod 600 .telegram
   sudo ./deploy-edge-server.sh
   ```
3. Open your bot in Telegram, send `/id`, put the number it answers into
   `TELEGRAM_ALLOWED_IDS` (several ids: comma-separated) and run the deploy again.

**Low-disk alerts.** Without being asked, the bot messages every user in
`TELEGRAM_ALLOWED_IDS` when the recordings or system disk has less than 15% free,
repeats the message every 6 hours while it stays low, and says so once when space is
back (above the level plus 3 points). Recordings and the system disk are one
alert when they share a filesystem. Tune it in `.telegram`:

```
ALERT_DISK_FREE_PERCENT=15   # 0 turns alerts off
ALERT_INTERVAL=300           # seconds between checks
ALERT_REPEAT_HOURS=6
```

How it is kept safe:

- **Only the listed user ids get answers.** Everyone else is ignored (a stranger can
  only learn their own id). Authorization is by the sender, not the chat.
- **No open port.** The bot polls Telegram, so the server only makes outbound HTTPS
  requests. The token stays in `.telegram` (mode 600), never in the compose file.
- **Locked-down container.** Read-only filesystem, all capabilities dropped,
  read-only mounts. It sees the host's processes (`pid: host`) so `/diag` can
  list what is using the CPU, but it has **no access to the Docker socket**, so
  the report has no `docker stats` section.
- **Reports leave your network.** `/diag` goes through Telegram's servers.
  Passwords are removed, but it lists private camera IPs and your Frigate config;
  use it only in a private chat with the bot.

Turn it off with `INSTALL_TELEGRAM_BOT=false` (the container is removed on the
next deploy); removing `.telegram` alone does not remove a running bot.

#### Reboot from Telegram (`/reboot`)

Off by default. A stolen phone or hijacked Telegram account must not be enough to
reboot the server, so the command needs a one-time code from an authenticator app
(TOTP, the same 6-digit codes as for 2-factor logins):

1. On any computer with Python: `python3 telegram-bot/bot.py --totp-setup`. It prints
   a secret, an `otpauth://` link and the code the app should show.
2. Add the secret to your authenticator app (Google Authenticator, Aegis, 2FAS, ...)
   by typing it in, or make a QR code from the link.
3. Put `REBOOT_TOTP_SECRET=<secret>` into `.telegram` on the server and run the deploy.
4. In a private chat with the bot send `/reboot 123456` (the current code). The bot
   deletes your message, answers "Rebooting now", and messages you when it is back.

Safeguards:

- Only listed user ids, only in a private chat, and the message must be under a minute old.
- A code works once (replays are refused, even after a restart) and drifts by at most
  one 30-second step. Three wrong codes lock that user out for 15 minutes.
- No reboot loops: a laptop that booted less than 10 minutes ago is not rebooted, and
  Telegram re-delivering an old command does nothing because of the age and replay checks.
- The bot container still has no power over the host. It can only create a file in
  `bot-requests/` (a root-only folder); a systemd unit (`edge-reboot.path`) on the host
  sees the file and runs `systemctl reboot`. Look at it with
  `systemctl status edge-reboot.path edge-reboot.service`.
- Remove `REBOOT_TOTP_SECRET` from `.telegram` and redeploy to switch it off again; the
  deploy removes the systemd units.

Keep the secret like a password. Anyone who has it can generate valid codes (but
still needs to be one of the listed Telegram users). Test it once while you are
sitting at the laptop.

### Performance diagnostics

If the server is slow or the CPU is pegged, collect a report (hardware, power and
thermal state, load, what each `ffmpeg` is doing, Frigate's detector speed and
per-camera FPS, the Frigate config) with:

```bash
sudo ./collect-diagnostics.sh        # about 25 seconds; writes edge-diagnostics-<time>.txt
```

Camera and other passwords are removed from the report, but it lists private IPs
and your config, so read it before sharing.

### Updating an existing server

Re-running the deploy script updates images, compose file, firewall and
services in place:

```bash
git pull
eval "$(./secrets.sh export)"   # only needed with INSTALL_MQTT=true
sudo -E ./deploy-edge-server.sh
```

Your Frigate config (`frigate/config.yml`) and Home Assistant
`configuration.yaml` are kept; the latest Frigate template is written next to
it as `frigate/config.yml.new` so you can compare. To regenerate the Frigate
config from the template instead, run with `FRIGATE_CONFIG_OVERWRITE=true`
(the old file is saved as `config.yml.bak.<timestamp>`).

Hardware video decoding (Intel VA-API) is **off by default**, because it fails
on some GPUs and camera streams. Frigate decodes in software, which always works.
To try it, deploy with `ENABLE_HWACCEL=true` (needs `/dev/dri/renderD128`).

Container logs are rotated (10 MB x 3 files per container); Docker keeps them
forever otherwise. Containers started before this setting have unbounded logs:
the next deploy recreates them (Frigate restarts once) and the old log goes with
the old container. To see their size first:
`sudo du -sh /var/lib/docker/containers/*/*-json.log`.

## 6. Post-Deployment

| Service | URL |
|---------|-----|
| Frigate | `http://<ip>:5000` |
| Home Assistant (if installed) | `http://<ip>:8123` |
| MQTT (if installed) | `<ip>:1883` |

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

**Plugins:**
- `plugins/victron-shunt/` — Victron Smart Shunt BLE monitor
- `plugins/renogy-rover/` — Renogy Rover MPPT BLE monitor
- `plugins/nut-power/` — NUT-based power loss monitor

**Configuration:**
- `bootstrap.cfg.example` — Configuration template
- `DEPLOYMENT.md` — Detailed deployment reference

## Plugin Installation

### Victron Smart Shunt

```bash
# Local CLI testing
make victron-setup
make victron-scan        # Find devices
victron-shunt read --address XX:XX:XX:XX:XX:XX --key YOUR_KEY

# Install as systemd service
make victron-install
sudo nano /etc/victron-shunt/config.yaml  # Add address and key
sudo systemctl restart victron-shunt
```

### Renogy Rover MPPT

```bash
# Local CLI testing
make renogy-setup
make renogy-scan         # Find BT-2 modules
renogy-rover read --address XX:XX:XX:XX:XX:XX

# Install as systemd service
make renogy-install
sudo nano /etc/renogy-rover/config.yaml  # Add address
sudo systemctl restart renogy-rover
```

### NUT Power Monitor

```bash
# Install on laptop (auto-shutdown on power loss)
make nut-install
make nut-status          # Check battery status
```

## Development

```bash
# Setup (creates venv and installs dependencies)
make setup

# Run Python tests
make pytest

# Run all tests
make test-scripts   # Shell script tests
make pytest         # Python unit tests
make test           # Docker integration tests

# Same lint/type/coverage checks as CI
make ci-local

# Plugin tests
make victron-test
make renogy-test

# HA Integration test (local Docker)
make ha-test         # Start HA + MQTT + simulator
make ha-test-stop    # Stop test environment
```

### CI

`.github/workflows/test.yml` runs on every push/PR to `main` using only
GitHub-hosted runners and GitHub's own actions (no external services):

| Job | What it checks |
|-----|----------------|
| Python tests | `pytest` on 3.10–3.13 with branch coverage (fails under 85%); the suite also exercises `secrets.sh`, the deploy scripts' generated configs and the NUT poller |
| Python lint & types | black, ruff, mypy |
| Plugin tests | victron-shunt and renogy-rover tests + ruff (fails under 75% coverage) |
| Shell lint & tests | `bash -n`, ShellCheck on every script, `tests/test-scripts.sh` |
| Docker Compose | `docker compose config` for every compose file |
| Integration | starts the lite stack and checks Mosquitto + Frigate |

Coverage is shown in each run's job summary and the HTML report is attached
as the `coverage-core` artifact.

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
