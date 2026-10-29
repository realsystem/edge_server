"""Read data from Victron Smart Shunt via BLE."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

try:
    from bleak import BleakScanner
except ImportError:
    BleakScanner = None

try:
    from victron_ble.devices import detect_device_type
except ImportError:
    detect_device_type = None

log = logging.getLogger(__name__)


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


VICTRON_MANUFACTURER_ID = 0x02E1


def _call(parsed: Any, method: str) -> Any:
    fn = getattr(parsed, method, None)
    return fn() if callable(fn) else None


def parse_advertisement(manufacturer_data: Dict[int, bytes], encryption_key: str) -> Optional[ShuntReading]:
    """Decode a Victron "instant readout" advertisement into a ShuntReading.

    Returns None when the advertisement is not from a Victron device or is of a
    type victron-ble does not understand.
    """
    if detect_device_type is None:
        raise RuntimeError("victron-ble not installed")

    raw_bytes = (manufacturer_data or {}).get(VICTRON_MANUFACTURER_ID)
    if raw_bytes is None:
        return None

    device_class = detect_device_type(raw_bytes)
    if device_class is None:
        return None

    parsed = device_class(encryption_key).parse(raw_bytes)

    voltage = _call(parsed, "get_voltage") or 0.0
    current = _call(parsed, "get_current") or 0.0
    soc = _call(parsed, "get_soc") or 0.0
    model = _call(parsed, "get_model_name")

    return ShuntReading(
        voltage=voltage,
        current=current,
        soc=soc,
        power=voltage * current,
        consumed_ah=_call(parsed, "get_consumed_ah"),
        time_remaining=_call(parsed, "get_remaining_mins"),
        raw_data={"model": model} if model else None,
    )


class VictronReader:
    """Read encrypted BLE advertisements from Victron devices."""

    def __init__(self, address: str, encryption_key: str):
        self.address = address.upper()
        self.encryption_key = encryption_key
        self._last_reading: Optional[ShuntReading] = None

    def _check_deps(self) -> None:
        if BleakScanner is None:
            raise RuntimeError("bleak not installed")
        if detect_device_type is None:
            raise RuntimeError("victron-ble not installed")

    def _parse(self, device: Any, adv_data: Any) -> Optional[ShuntReading]:
        if device.address.upper() != self.address:
            return None
        try:
            return parse_advertisement(adv_data.manufacturer_data, self.encryption_key)
        except Exception as e:
            log.debug("Failed to parse advertisement: %s", e)
            return None

    async def read_once(self, timeout: float = 30.0) -> Optional[ShuntReading]:
        """Read a single advertisement from the device."""
        self._check_deps()

        result: Optional[ShuntReading] = None
        event = asyncio.Event()

        def callback(device: Any, adv_data: Any) -> None:
            nonlocal result
            reading = self._parse(device, adv_data)
            if reading is not None:
                result = reading
                event.set()

        scanner = BleakScanner(detection_callback=callback)
        await scanner.start()

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            await scanner.stop()

        return result

    def _is_new(self, reading: ShuntReading) -> bool:
        last = self._last_reading
        return (
            last is None
            or reading.voltage != last.voltage
            or reading.current != last.current
            or reading.soc != last.soc
        )

    async def read_continuous(
        self,
        callback: Callable[[ShuntReading], None],
        interval: float = 1.0,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Continuously read and call callback with new readings."""
        self._check_deps()

        def ble_callback(device: Any, adv_data: Any) -> None:
            reading = self._parse(device, adv_data)
            if reading is not None and self._is_new(reading):
                self._last_reading = reading
                callback(reading)

        scanner = BleakScanner(detection_callback=ble_callback)
        await scanner.start()

        try:
            while True:
                if stop_event and stop_event.is_set():
                    break
                await asyncio.sleep(interval)
        finally:
            await scanner.stop()
