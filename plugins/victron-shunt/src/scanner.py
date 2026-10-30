"""Bluetooth scanner for Victron devices."""

import asyncio
import sys
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

try:
    from bleak import BleakScanner
except ImportError:
    BleakScanner = None


@dataclass
class BluetoothStatus:
    """Bluetooth adapter status."""

    available: bool
    error: Optional[str] = None
    adapter: Optional[str] = None


@dataclass
class VictronDevice:
    """Discovered Victron device."""

    name: str
    address: str
    rssi: int
    model: Optional[str] = None


VICTRON_PREFIXES = ("Smart Shunt", "SmartShunt", "BMV", "Smart Battery", "Orion")


async def check_bluetooth() -> BluetoothStatus:
    """Check if Bluetooth is available and working."""
    if BleakScanner is None:
        return BluetoothStatus(
            available=False, error="bleak not installed (pip install bleak)"
        )

    try:
        scanner = BleakScanner()
        await asyncio.wait_for(scanner.start(), timeout=5.0)
        await scanner.stop()
        return BluetoothStatus(available=True, adapter="default")
    except asyncio.TimeoutError:
        return BluetoothStatus(available=False, error="Bluetooth adapter not responding")
    except Exception as e:
        error_msg = str(e)
        if "Bluetooth" in error_msg or "adapter" in error_msg.lower():
            return BluetoothStatus(available=False, error=error_msg)
        if sys.platform == "linux" and "org.bluez" in error_msg:
            return BluetoothStatus(
                available=False,
                error="BlueZ not running. Try: sudo systemctl start bluetooth",
            )
        return BluetoothStatus(available=False, error=f"BLE error: {error_msg}")


async def scan_victron_devices(timeout: float = 10.0) -> List[VictronDevice]:
    """Scan for Victron BLE devices."""
    if BleakScanner is None:
        return []

    devices: List[VictronDevice] = []
    seen_addresses: Set[str] = set()

    def callback(device, adv_data):
        if device.address in seen_addresses:
            return
        name = device.name or adv_data.local_name or ""
        if any(name.startswith(prefix) for prefix in VICTRON_PREFIXES):
            seen_addresses.add(device.address)
            model = None
            for prefix in VICTRON_PREFIXES:
                if name.startswith(prefix):
                    model = prefix
                    break
            devices.append(
                VictronDevice(
                    name=name,
                    address=device.address,
                    rssi=adv_data.rssi or -100,
                    model=model,
                )
            )

    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    return sorted(devices, key=lambda d: d.rssi, reverse=True)


async def scan_all_devices(timeout: float = 10.0) -> List[Tuple[str, str, int]]:
    """Scan all BLE devices (for debugging). Returns (name, address, rssi)."""
    if BleakScanner is None:
        return []

    # BLEDevice.rssi was removed in bleak 1.0; RSSI lives in AdvertisementData.
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results = [
        (dev.name or adv.local_name or "(unknown)", dev.address, adv.rssi or -100)
        for dev, adv in found.values()
    ]
    return sorted(results, key=lambda r: r[2], reverse=True)
