"""Read data from Victron Smart Shunt via BLE."""

import asyncio
from dataclasses import dataclass
from typing import Optional

try:
    from bleak import BleakScanner
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:
    BleakScanner = None

try:
    from victron_ble.devices import detect_device_type
except ImportError:
    detect_device_type = None


@dataclass
class ShuntReading:
    """Battery data from Smart Shunt."""

    voltage: float  # Volts
    current: float  # Amps (positive = charging)
    soc: float  # State of charge 0-100%
    power: float  # Watts
    consumed_ah: Optional[float] = None
    time_remaining: Optional[int] = None  # Minutes
    raw_data: Optional[dict] = None


class VictronReader:
    """Read encrypted BLE advertisements from Victron devices."""

    def __init__(self, address: str, encryption_key: str):
        self.address = address.upper()
        self.encryption_key = encryption_key
        self._last_reading: Optional[ShuntReading] = None

    async def read_once(self, timeout: float = 30.0) -> Optional[ShuntReading]:
        """Read a single advertisement from the device."""
        if BleakScanner is None:
            raise RuntimeError("bleak not installed")
        if detect_device_type is None:
            raise RuntimeError("victron-ble not installed")

        result: Optional[ShuntReading] = None
        event = asyncio.Event()

        def callback(device: BLEDevice, adv_data: AdvertisementData):
            nonlocal result
            if device.address.upper() != self.address:
                return

            try:
                raw_bytes = None
                if adv_data.manufacturer_data:
                    for mfr_id, data in adv_data.manufacturer_data.items():
                        if mfr_id == 0x02E1:  # Victron manufacturer ID
                            raw_bytes = data
                            break

                if raw_bytes is None:
                    return

                device_class = detect_device_type(raw_bytes)
                if device_class is None:
                    return

                device_instance = device_class(self.encryption_key)
                parsed = device_instance.parse(raw_bytes)

                result = ShuntReading(
                    voltage=getattr(parsed, "voltage", 0.0) or 0.0,
                    current=getattr(parsed, "current", 0.0) or 0.0,
                    soc=getattr(parsed, "soc", 0.0) or 0.0,
                    power=(getattr(parsed, "voltage", 0.0) or 0.0)
                    * (getattr(parsed, "current", 0.0) or 0.0),
                    consumed_ah=getattr(parsed, "consumed_ah", None),
                    time_remaining=getattr(parsed, "remaining_mins", None),
                    raw_data=vars(parsed) if hasattr(parsed, "__dict__") else None,
                )
                event.set()

            except Exception as e:
                print(f"Parse error: {e}")

        scanner = BleakScanner(detection_callback=callback)
        await scanner.start()

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            await scanner.stop()

        return result

    async def read_continuous(self, callback, interval: float = 1.0):
        """Continuously read and call callback with new readings."""
        if BleakScanner is None:
            raise RuntimeError("bleak not installed")
        if detect_device_type is None:
            raise RuntimeError("victron-ble not installed")

        def ble_callback(device: BLEDevice, adv_data: AdvertisementData):
            if device.address.upper() != self.address:
                return

            try:
                raw_bytes = None
                if adv_data.manufacturer_data:
                    for mfr_id, data in adv_data.manufacturer_data.items():
                        if mfr_id == 0x02E1:
                            raw_bytes = data
                            break

                if raw_bytes is None:
                    return

                device_class = detect_device_type(raw_bytes)
                if device_class is None:
                    return

                device_instance = device_class(self.encryption_key)
                parsed = device_instance.parse(raw_bytes)

                reading = ShuntReading(
                    voltage=getattr(parsed, "voltage", 0.0) or 0.0,
                    current=getattr(parsed, "current", 0.0) or 0.0,
                    soc=getattr(parsed, "soc", 0.0) or 0.0,
                    power=(getattr(parsed, "voltage", 0.0) or 0.0)
                    * (getattr(parsed, "current", 0.0) or 0.0),
                    consumed_ah=getattr(parsed, "consumed_ah", None),
                    time_remaining=getattr(parsed, "remaining_mins", None),
                )

                if (
                    self._last_reading is None
                    or reading.voltage != self._last_reading.voltage
                    or reading.current != self._last_reading.current
                ):
                    self._last_reading = reading
                    callback(reading)

            except Exception:
                pass

        scanner = BleakScanner(detection_callback=ble_callback)
        await scanner.start()

        try:
            while True:
                await asyncio.sleep(interval)
        finally:
            await scanner.stop()
