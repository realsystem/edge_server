"""CLI for Victron Smart Shunt BLE reader."""

import asyncio
import sys
from pathlib import Path

import click

from .config import CONFIG_PATHS, Config
from .reader import VictronReader
from .scanner import check_bluetooth, scan_all_devices, scan_victron_devices


def run_async(coro):
    """Run async coroutine."""
    return asyncio.get_event_loop().run_until_complete(coro)


pass_config = click.make_pass_decorator(Config, ensure=True)


@click.group()
@click.option("--config", "-c", "config_path", type=click.Path(exists=True, path_type=Path),
              help="Config file path (default: ~/.config/victron-shunt.yaml)")
@click.version_option()
@click.pass_context
def cli(ctx, config_path):
    """Victron Smart Shunt BLE reader.

    Configuration is loaded from ~/.config/victron-shunt.yaml or /etc/victron-shunt/config.yaml.
    Environment variables (VICTRON_ADDRESS, VICTRON_KEY, MQTT_*) override config file values.
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
    """Scan for Victron BLE devices."""
    click.echo(f"Scanning for {'all BLE' if show_all else 'Victron'} devices ({timeout}s)...")

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
        devices = run_async(scan_victron_devices(timeout))
        if not devices:
            click.echo("No Victron devices found")
            click.echo("\nTips:")
            click.echo("  - Move closer to the Smart Shunt")
            click.echo("  - Make sure Bluetooth is enabled on the device")
            click.echo("  - Try 'victron-shunt scan --all' to see all BLE devices")
            sys.exit(1)

        click.echo(f"\nFound {len(devices)} Victron device(s):\n")
        for dev in devices:
            click.secho(f"  {dev.name}", fg="green")
            click.echo(f"    Address: {dev.address}")
            click.echo(f"    Signal:  {dev.rssi} dBm")
            click.echo()

        click.echo("To save config and read data:")
        click.echo(f"  victron-shunt config --address {devices[0].address} --key <YOUR_KEY>")
        click.echo("  victron-shunt read")


@cli.command()
@click.option("--address", "-a", help="Device MAC address")
@click.option("--key", "-k", help="Encryption key (hex)")
@click.option("--timeout", "-t", default=30.0, help="Read timeout in seconds")
@click.option("--continuous", "-c", is_flag=True, help="Continuous reading mode")
@click.pass_context
def read(ctx, address: str, key: str, timeout: float, continuous: bool):
    """Read data from Victron Smart Shunt."""
    config: Config = ctx.obj["config"]

    # CLI args override config
    address = address or config.address
    key = key or config.key

    if not address or not key:
        click.secho("Error: address and key required", fg="red")
        click.echo("\nProvide via:")
        click.echo("  1. Config file: victron-shunt config --address XX --key YY")
        click.echo("  2. CLI args: victron-shunt read --address XX --key YY")
        click.echo("  3. Env vars: VICTRON_ADDRESS, VICTRON_KEY")
        sys.exit(1)

    key = key.replace(" ", "").replace("-", "").replace(":", "")
    if len(key) != 32:
        click.secho(f"Invalid key length: {len(key)} (expected 32 hex chars)", fg="red")
        sys.exit(1)

    reader = VictronReader(address, key)

    if continuous:
        click.echo(f"Reading from {address} (Ctrl+C to stop)...\n")

        def on_reading(r):
            click.echo(
                f"V: {r.voltage:6.2f}V | "
                f"I: {r.current:+7.2f}A | "
                f"P: {r.power:+8.1f}W | "
                f"SoC: {r.soc:5.1f}%"
            )

        try:
            run_async(reader.read_continuous(on_reading))
        except KeyboardInterrupt:
            click.echo("\nStopped")
    else:
        click.echo(f"Reading from {address} (timeout {timeout}s)...")

        reading = run_async(reader.read_once(timeout))

        if reading is None:
            click.secho("No data received", fg="red")
            click.echo("\nPossible issues:")
            click.echo("  - Wrong MAC address")
            click.echo("  - Wrong encryption key")
            click.echo("  - Device not advertising (wake it up)")
            click.echo("  - Out of range")
            sys.exit(1)

        click.echo()
        click.secho("Battery Status:", fg="green", bold=True)
        click.echo(f"  Voltage:     {reading.voltage:.2f} V")
        click.echo(f"  Current:     {reading.current:+.2f} A")
        click.echo(f"  Power:       {reading.power:+.1f} W")
        click.echo(f"  State of Charge: {reading.soc:.1f}%")

        if reading.consumed_ah is not None:
            click.echo(f"  Consumed:    {reading.consumed_ah:.1f} Ah")
        if reading.time_remaining is not None:
            hours = reading.time_remaining // 60
            mins = reading.time_remaining % 60
            click.echo(f"  Time Left:   {hours}h {mins}m")

        if reading.raw_data:
            click.echo("\nRaw data:")
            for k, v in reading.raw_data.items():
                if not k.startswith("_"):
                    click.echo(f"  {k}: {v}")


@cli.command("config")
@click.option("--address", "-a", help="Device MAC address")
@click.option("--key", "-k", help="Encryption key (hex)")
@click.option("--mqtt-host", help="MQTT broker host")
@click.option("--mqtt-port", type=int, help="MQTT broker port")
@click.option("--mqtt-user", help="MQTT username")
@click.option("--show", is_flag=True, help="Show current config")
@click.pass_context
def config_cmd(ctx, address, key, mqtt_host, mqtt_port, mqtt_user, show):
    """Show or update configuration."""
    config: Config = ctx.obj["config"]

    if show or (not address and not key and not mqtt_host and not mqtt_port and not mqtt_user):
        # Show current config
        click.echo("Configuration:")
        click.echo(f"  Address: {config.address or '(not set)'}")
        click.echo(f"  Key:     {'*' * 8 + config.key[-8:] if config.key else '(not set)'}")
        click.echo(f"  MQTT:    {config.mqtt.host}:{config.mqtt.port}")
        if config.mqtt.user:
            click.echo(f"  MQTT user: {config.mqtt.user}")
        click.echo()
        click.echo("Config files searched:")
        for path in CONFIG_PATHS:
            exists = " (found)" if path.exists() else ""
            click.echo(f"  {path}{exists}")
        click.echo()
        click.echo("Environment variables:")
        click.echo("  VICTRON_ADDRESS, VICTRON_KEY")
        click.echo("  MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS")
        return

    # Update config
    if address:
        config.address = address
    if key:
        config.key = key.replace(" ", "").replace("-", "").replace(":", "")
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
    click.echo("Test with: victron-shunt read")


@cli.command()
def info():
    """Show how to get the encryption key."""
    click.echo("Getting the encryption key from Victron Connect app:\n")
    click.echo("  1. Open Victron Connect on your phone")
    click.echo("  2. Connect to your Smart Shunt")
    click.echo("  3. Tap the gear icon (Settings)")
    click.echo("  4. Scroll to 'Product Info'")
    click.echo("  5. Find 'Encryption data' - tap to reveal")
    click.echo("  6. Copy the 32-character hex key")
    click.echo()
    click.echo("The key looks like: 0df4d0395b7d1a876c0c33ecb9e70dcd")
    click.echo()
    click.echo("Note: Keep this key secret - it allows reading your battery data")


def main():
    cli()


if __name__ == "__main__":
    main()
