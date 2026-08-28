"""CLI for Victron Smart Shunt BLE reader."""

import asyncio
import sys

import click

from .reader import VictronReader
from .scanner import check_bluetooth, scan_all_devices, scan_victron_devices


def run_async(coro):
    """Run async coroutine."""
    return asyncio.get_event_loop().run_until_complete(coro)


@click.group()
@click.version_option()
def cli():
    """Victron Smart Shunt BLE reader.

    First run 'check' to verify Bluetooth, then 'scan' to find devices,
    then 'read' with the device address and encryption key.
    """
    pass


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

        click.echo("To read data, get encryption key from Victron Connect app:")
        click.echo("  Device > Settings (gear) > Product Info > Encryption data")
        click.echo()
        click.echo("Then run:")
        click.echo(f"  victron-shunt read --address {devices[0].address} --key <YOUR_KEY>")


@cli.command()
@click.option("--address", "-a", required=True, help="Device MAC address")
@click.option("--key", "-k", required=True, help="Encryption key (hex)")
@click.option("--timeout", "-t", default=30.0, help="Read timeout in seconds")
@click.option("--continuous", "-c", is_flag=True, help="Continuous reading mode")
def read(address: str, key: str, timeout: float, continuous: bool):
    """Read data from Victron Smart Shunt."""
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
