"""CLI for Renogy Rover MPPT BLE reader."""

import asyncio
import sys
from pathlib import Path

import click

from .config import CONFIG_PATHS, Config
from .reader import RenogyReader
from .scanner import check_bluetooth, scan_all_devices, scan_renogy_devices


def run_async(coro):
    """Run async coroutine."""
    return asyncio.get_event_loop().run_until_complete(coro)


pass_config = click.make_pass_decorator(Config, ensure=True)


@click.group()
@click.option("--config", "-c", "config_path", type=click.Path(exists=True, path_type=Path),
              help="Config file path (default: ~/.config/renogy-rover.yaml)")
@click.version_option()
@click.pass_context
def cli(ctx, config_path):
    """Renogy Rover MPPT BLE reader.

    Configuration is loaded from ~/.config/renogy-rover.yaml or /etc/renogy-rover/config.yaml.
    Environment variables (RENOGY_ADDRESS, MQTT_*) override config file values.
    CLI arguments override everything.
    """
    ctx.ensure_object(dict)
    ctx.obj["config"] = Config.load(config_path)


@cli.command()
def check():
    """Check if Bluetooth is available."""
    click.echo("Checking Bluetooth adapter...")

    status = run_async(check_bluetooth())

    if status.available:
        click.secho("Bluetooth is available", fg="green")
        if status.adapter:
            click.echo(f"  Adapter: {status.adapter}")
        sys.exit(0)
    else:
        click.secho("Bluetooth not available", fg="red")
        if status.error:
            click.echo(f"  Error: {status.error}")

        if sys.platform == "linux":
            click.echo("\nTroubleshooting:")
            click.echo("  1. Check adapter: hciconfig -a")
            click.echo("  2. Start Bluetooth: sudo systemctl start bluetooth")
            click.echo("  3. Install BlueZ: sudo apt install bluez")
        elif sys.platform == "darwin":
            click.echo("\nTroubleshooting:")
            click.echo("  1. System Preferences > Bluetooth > Turn On")
            click.echo("  2. Grant terminal Bluetooth permission if prompted")

        sys.exit(1)


@cli.command()
@click.option("--timeout", "-t", default=10.0, help="Scan timeout in seconds")
@click.option("--all", "show_all", is_flag=True, help="Show all BLE devices")
def scan(timeout: float, show_all: bool):
    """Scan for Renogy BLE devices (BT-1/BT-2 modules)."""
    click.echo(f"Scanning for {'all BLE' if show_all else 'Renogy'} devices ({timeout}s)...")

    if show_all:
        devices = run_async(scan_all_devices(timeout))
        if not devices:
            click.echo("No BLE devices found")
            sys.exit(1)

        click.echo(f"\nFound {len(devices)} devices:\n")
        click.echo(f"{'Name':<30} {'Address':<20} {'RSSI'}")
        click.echo("-" * 60)
        for name, address, rssi in devices:
            click.echo(f"{name:<30} {address:<20} {rssi} dBm")
    else:
        devices = run_async(scan_renogy_devices(timeout))
        if not devices:
            click.echo("No Renogy devices found")
            click.echo("\nTips:")
            click.echo("  - Move closer to the MPPT controller")
            click.echo("  - Make sure BT-1/BT-2 module is installed")
            click.echo("  - Try 'renogy-rover scan --all' to see all BLE devices")
            sys.exit(1)

        click.echo(f"\nFound {len(devices)} Renogy device(s):\n")
        for dev in devices:
            click.secho(f"  {dev.name}", fg="green")
            click.echo(f"    Address: {dev.address}")
            click.echo(f"    Signal:  {dev.rssi} dBm")
            click.echo()

        click.echo("To save config and read data:")
        click.echo(f"  renogy-rover config --address {devices[0].address}")
        click.echo("  renogy-rover read")


@cli.command()
@click.option("--address", "-a", help="Device MAC address")
@click.option("--timeout", "-t", default=30.0, help="Read timeout in seconds")
@click.option("--continuous", "-c", is_flag=True, help="Continuous reading mode")
@click.option("--interval", "-i", default=30.0, help="Poll interval in seconds for continuous mode")
@click.option("--mqtt", is_flag=True, help="Publish to MQTT broker")
@click.pass_context
def read(ctx, address: str, timeout: float, continuous: bool, interval: float, mqtt: bool):
    """Read data from Renogy Rover MPPT."""
    config: Config = ctx.obj["config"]

    # CLI args override config
    address = address or config.address

    if not address:
        click.secho("Error: address required", fg="red")
        click.echo("\nProvide via:")
        click.echo("  1. Config file: renogy-rover config --address XX:XX:XX:XX:XX:XX")
        click.echo("  2. CLI args: renogy-rover read --address XX:XX:XX:XX:XX:XX")
        click.echo("  3. Env var: RENOGY_ADDRESS")
        sys.exit(1)

    # Setup MQTT if requested
    mqtt_pub = None
    if mqtt:
        from .mqtt import MQTTPublisher
        click.echo(f"Connecting to MQTT broker {config.mqtt.host}:{config.mqtt.port}...")
        try:
            mqtt_pub = MQTTPublisher(config.mqtt)
            mqtt_pub.connect()
            mqtt_pub.publish_discovery()
            click.secho("MQTT connected, discovery published", fg="green")
        except Exception as e:
            click.secho(f"MQTT connection failed: {e}", fg="red")
            sys.exit(1)

    reader = RenogyReader(address, device_id=config.device_id)

    if continuous:
        click.echo(f"Reading from {address} every {interval}s (Ctrl+C to stop)...\n")

        stop_event = asyncio.Event()

        def on_reading(r):
            click.echo(
                f"Bat: {r.battery_voltage:5.1f}V {r.battery_soc:3d}% | "
                f"PV: {r.pv_voltage:5.1f}V {r.pv_power:6.1f}W | "
                f"State: {r.charge_state}"
            )
            if mqtt_pub:
                mqtt_pub.publish_reading(r)

        try:
            run_async(reader.poll_continuous(on_reading, interval=interval, stop_event=stop_event))
        except KeyboardInterrupt:
            click.echo("\nStopped")
        finally:
            if mqtt_pub:
                mqtt_pub.disconnect()
    else:
        click.echo(f"Reading from {address} (timeout {timeout}s)...")

        reading = reader.read_once_sync(timeout)

        if reading is None:
            click.secho("No data received", fg="red")
            click.echo("\nPossible issues:")
            click.echo("  - Wrong MAC address")
            click.echo("  - BT-2 module not paired/configured")
            click.echo("  - Device out of range")
            if mqtt_pub:
                mqtt_pub.disconnect()
            sys.exit(1)

        if mqtt_pub:
            mqtt_pub.publish_reading(reading)
            mqtt_pub.disconnect()
            click.secho("Published to MQTT", fg="green")

        click.echo()
        click.secho("Battery:", fg="green", bold=True)
        click.echo(f"  Voltage:     {reading.battery_voltage:.2f} V")
        click.echo(f"  Current:     {reading.battery_current:+.2f} A")
        click.echo(f"  SoC:         {reading.battery_soc}%")
        if reading.battery_temp is not None:
            click.echo(f"  Temperature: {reading.battery_temp}°C")

        click.echo()
        click.secho("Solar Panel:", fg="yellow", bold=True)
        click.echo(f"  Voltage:     {reading.pv_voltage:.2f} V")
        click.echo(f"  Current:     {reading.pv_current:.2f} A")
        click.echo(f"  Power:       {reading.pv_power:.1f} W")

        click.echo()
        click.secho("Controller:", fg="cyan", bold=True)
        click.echo(f"  Charge State: {reading.charge_state}")
        if reading.controller_temp is not None:
            click.echo(f"  Temperature:  {reading.controller_temp}°C")
        click.echo(f"  Daily Energy: {reading.daily_energy:.1f} Wh")

        if reading.load_power > 0 or reading.load_enabled:
            click.echo()
            click.secho("Load:", fg="magenta", bold=True)
            click.echo(f"  Power:    {reading.load_power:.1f} W")
            click.echo(f"  Enabled:  {'Yes' if reading.load_enabled else 'No'}")

        if reading.raw_data:
            click.echo("\nRaw data:")
            for k, v in sorted(reading.raw_data.items()):
                if not k.startswith("_"):
                    click.echo(f"  {k}: {v}")


@cli.command("config")
@click.option("--address", "-a", help="Device MAC address")
@click.option("--device-id", type=int, help="Modbus device ID (default: 255)")
@click.option("--poll-interval", type=int, help="Poll interval in seconds")
@click.option("--mqtt-host", help="MQTT broker host")
@click.option("--mqtt-port", type=int, help="MQTT broker port")
@click.option("--mqtt-user", help="MQTT username")
@click.option("--show", is_flag=True, help="Show current config")
@click.pass_context
def config_cmd(ctx, address, device_id, poll_interval, mqtt_host, mqtt_port, mqtt_user, show):
    """Show or update configuration."""
    config: Config = ctx.obj["config"]

    if show or (not address and device_id is None and poll_interval is None
                and not mqtt_host and not mqtt_port and not mqtt_user):
        # Show current config
        click.echo("Configuration:")
        click.echo(f"  Address:       {config.address or '(not set)'}")
        click.echo(f"  Device ID:     {config.device_id}")
        click.echo(f"  Poll Interval: {config.poll_interval}s")
        click.echo(f"  MQTT:          {config.mqtt.host}:{config.mqtt.port}")
        if config.mqtt.user:
            click.echo(f"  MQTT user:     {config.mqtt.user}")
        click.echo()
        click.echo("Config files searched:")
        for path in CONFIG_PATHS:
            exists = " (found)" if path.exists() else ""
            click.echo(f"  {path}{exists}")
        click.echo()
        click.echo("Environment variables:")
        click.echo("  RENOGY_ADDRESS, RENOGY_DEVICE_ID, RENOGY_POLL_INTERVAL")
        click.echo("  MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS")
        return

    # Update config
    if address:
        config.address = address
    if device_id is not None:
        config.device_id = device_id
    if poll_interval is not None:
        config.poll_interval = poll_interval
    if mqtt_host:
        config.mqtt.host = mqtt_host
    if mqtt_port:
        config.mqtt.port = mqtt_port
    if mqtt_user:
        config.mqtt.user = mqtt_user

    # Validate
    errors = config.validate()
    if errors:
        click.secho("Config validation errors:", fg="yellow")
        for err in errors:
            click.echo(f"  - {err}")
        click.echo()

    # Save to user config
    config_path = CONFIG_PATHS[0]
    config.save(config_path)
    click.secho(f"Config saved to {config_path}", fg="green")
    click.echo()
    click.echo("Test with: renogy-rover read")


@cli.command()
def info():
    """Show information about Renogy BT-1/BT-2 modules."""
    click.echo("Renogy BT-1 / BT-2 Bluetooth Modules:\n")
    click.echo("  BT-1: Original module, connects via RS-232")
    click.echo("  BT-2: Newer module, connects via RS-485 or RJ12")
    click.echo()
    click.echo("Setup:")
    click.echo("  1. Install the BT-2 module in your MPPT controller")
    click.echo("  2. Download Renogy DC Home app on your phone")
    click.echo("  3. Pair the device in the app first")
    click.echo("  4. Scan for the device: renogy-rover scan")
    click.echo("  5. Configure: renogy-rover config --address XX:XX:XX:XX:XX:XX")
    click.echo()
    click.echo("Compatible controllers:")
    click.echo("  - Rover series (20A, 30A, 40A, 60A)")
    click.echo("  - Wanderer series")
    click.echo("  - Adventurer series")


@cli.command()
@click.option("--no-mqtt", is_flag=True, help="Run without MQTT (logging only)")
@click.pass_context
def service(ctx, no_mqtt: bool):
    """Run as a service (continuous monitoring, optional MQTT).

    Designed to run under systemd. Reads config from file/env vars.
    Logs to stdout for journald capture.
    """
    import logging
    import signal
    import time as time_module

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("renogy-rover")

    config: Config = ctx.obj["config"]

    errors = config.validate()
    if errors:
        for err in errors:
            log.error(f"Config error: {err}")
        sys.exit(1)

    # Setup MQTT (optional)
    mqtt_pub = None
    if not no_mqtt and config.mqtt.host:
        from .mqtt import MQTTPublisher

        log.info(f"Connecting to MQTT {config.mqtt.host}:{config.mqtt.port}")
        try:
            mqtt_pub = MQTTPublisher(config.mqtt)
            mqtt_pub.connect()
            mqtt_pub.publish_discovery()
            log.info("MQTT connected, HA discovery published")
        except Exception as e:
            log.warning(f"MQTT connection failed: {e} - continuing without MQTT")
            mqtt_pub = None
    else:
        log.info("Running without MQTT (logging only)")

    # Setup reader
    reader = RenogyReader(config.address, device_id=config.device_id)
    log.info(f"Monitoring Renogy device {config.address} (poll interval: {config.poll_interval}s)")

    # Stop event for graceful shutdown
    stop_event = asyncio.Event()

    def handle_signal(signum, frame):
        log.info(f"Received signal {signum}, shutting down")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    start_time = time_module.time()
    readings_count = 0
    last_log = 0

    def format_uptime(seconds):
        days, remainder = divmod(int(seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours}h"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"

    def on_reading(r):
        nonlocal readings_count, last_log
        if mqtt_pub:
            mqtt_pub.publish_reading(r)
        readings_count += 1

        now = time_module.time()
        uptime = format_uptime(now - start_time)
        log.info(
            f"Bat={r.battery_voltage:.1f}V/{r.battery_soc}% "
            f"PV={r.pv_voltage:.1f}V/{r.pv_power:.0f}W "
            f"State={r.charge_state} "
            f"(#{readings_count}, uptime: {uptime})"
        )
        last_log = now

    try:
        run_async(reader.poll_continuous(
            on_reading,
            interval=float(config.poll_interval),
            stop_event=stop_event
        ))
    except KeyboardInterrupt:
        pass
    finally:
        if mqtt_pub:
            mqtt_pub.disconnect()
        log.info("Service stopped")


def main():
    cli()


if __name__ == "__main__":
    main()
