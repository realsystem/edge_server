# NUT Power Monitor

Monitors laptop battery using NUT (Network UPS Tools) and automatically shuts down when running on battery power for too long.

## Features

- Monitors laptop battery as a "UPS"
- Configurable shutdown delay (default: 10 minutes on battery)
- Configurable low battery threshold (default: 10%)
- Logs power events to syslog
- Wall notifications on power loss

## Installation

```bash
# From edge-server root
sudo ./plugins/nut-power/install.sh

# Or with custom settings
sudo NUT_SHUTDOWN_DELAY=300 NUT_LOW_BATTERY=15 ./plugins/nut-power/install.sh
```

## Shutdown Triggers

The system will shutdown when EITHER condition is met:
1. **On battery for 10 minutes** (configurable via `NUT_SHUTDOWN_DELAY`)
2. **Battery drops below 20%** (configurable via `NUT_LOW_BATTERY`)

## Configuration

Edit `/etc/nut/upsmon.conf`:
```ini
# Seconds on battery before shutdown (default: 600 = 10 min)
ONBATTERYDELAY 600
```

Edit `/etc/systemd/system/battery-poller.service`:
```ini
# Low battery threshold
Environment=NUT_LOW_BATTERY=10
```

After changes:
```bash
sudo systemctl daemon-reload
sudo systemctl restart nut-monitor battery-poller
```

## Commands

```bash
# Show battery status
upsc laptop

# Monitor status
watch -n1 upsc laptop

# View logs
journalctl -u nut-monitor -f
journalctl -u battery-poller -f

# Service status
systemctl status nut-monitor
systemctl status battery-poller
```

## How It Works

1. `battery-poller.service` reads `/sys/class/power_supply/` every 30 seconds
2. Writes status to `/var/run/nut/laptop.dev` in NUT format
3. `nut-server` (upsd) reads the dummy device file
4. `nut-monitor` (upsmon) monitors status and triggers shutdown when:
   - On battery longer than `ONBATTERYDELAY` seconds
   - Battery drops below low threshold

## Uninstall

```bash
sudo ./plugins/nut-power/uninstall.sh
```

## Troubleshooting

**No battery detected:**
```bash
ls /sys/class/power_supply/
cat /sys/class/power_supply/BAT0/capacity
```

**NUT not starting:**
```bash
journalctl -u nut-server -n 50
cat /var/run/nut/laptop.dev
```

**Test shutdown (dry run):**
```bash
# Simulate low battery
echo "battery.charge: 5
ups.status: OB LB" | sudo tee /var/run/nut/laptop.dev
# Watch the logs
journalctl -u nut-monitor -f
```
