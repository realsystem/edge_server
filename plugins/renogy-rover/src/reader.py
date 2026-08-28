"""Read data from Renogy Rover MPPT via BLE (BT-2 module)."""

import asyncio
from dataclasses import dataclass
from typing import Optional, Callable

try:
    from renogybt import RoverClient, InverterClient, RoverHistoryClient
    from renogybt import DeviceType
except ImportError:
    RoverClient = None
    DeviceType = None


@dataclass
class RoverReading:
    """Solar charger data from Renogy Rover."""

    # Battery
    battery_voltage: float  # V
    battery_current: float  # A
    battery_soc: int  # %
    battery_temp: Optional[int] = None  # °C

    # Solar panel
    pv_voltage: float = 0.0  # V
    pv_current: float = 0.0  # A
    pv_power: float = 0.0  # W

    # Controller
    charge_state: str = "unknown"  # off, mppt, boost, float, equalize
    controller_temp: Optional[int] = None  # °C

    # Load
    load_voltage: float = 0.0  # V
    load_current: float = 0.0  # A
    load_power: float = 0.0  # W
    load_enabled: bool = False

    # Statistics
    daily_energy: float = 0.0  # Wh
    total_energy: float = 0.0  # kWh

    # Raw data
    raw_data: Optional[dict] = None


CHARGE_STATES = {
    0: "off",
    1: "charging",
    2: "mppt",
    3: "equalizing",
    4: "boost",
    5: "float",
    6: "current_limiting",
}


class RenogyReader:
    """Read data from Renogy Rover via BLE."""

    def __init__(self, address: str, device_id: int = 255):
        if RoverClient is None:
            raise RuntimeError("renogybt not installed (pip install renogybt)")

        self.address = address.upper()
        self.device_id = device_id
        self._last_reading: Optional[RoverReading] = None

    async def read_once(self, timeout: float = 30.0) -> Optional[RoverReading]:
        """Read data from the device once."""
        reading_data = {}
        event = asyncio.Event()

        def on_data(client, data):
            nonlocal reading_data
            reading_data = data
            event.set()

        def on_error(client, error):
            event.set()

        client = RoverClient(
            mac_addr=self.address,
            device_id=self.device_id,
            on_data_received=on_data,
            on_error=on_error,
        )

        try:
            client.connect()
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

        if not reading_data:
            return None

        return self._parse_reading(reading_data)

    def read_once_sync(self, timeout: float = 30.0) -> Optional[RoverReading]:
        """Synchronous version of read_once."""
        reading_data = {}
        event_done = False

        def on_data(client, data):
            nonlocal reading_data, event_done
            reading_data = data
            event_done = True

        def on_error(client, error):
            nonlocal event_done
            event_done = True

        client = RoverClient(
            mac_addr=self.address,
            device_id=self.device_id,
            on_data_received=on_data,
            on_error=on_error,
        )

        try:
            client.connect()
            # Wait for data
            import time
            start = time.time()
            while not event_done and (time.time() - start) < timeout:
                time.sleep(0.1)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

        if not reading_data:
            return None

        return self._parse_reading(reading_data)

    def _parse_reading(self, data: dict) -> RoverReading:
        """Parse raw data into RoverReading."""
        charge_state_code = data.get("charging_state", 0)
        charge_state = CHARGE_STATES.get(charge_state_code, f"unknown({charge_state_code})")

        return RoverReading(
            battery_voltage=data.get("battery_voltage", 0.0),
            battery_current=data.get("battery_current", 0.0),
            battery_soc=data.get("battery_percentage", 0),
            battery_temp=data.get("battery_temperature"),
            pv_voltage=data.get("pv_voltage", 0.0),
            pv_current=data.get("pv_current", 0.0),
            pv_power=data.get("pv_power", 0.0),
            charge_state=charge_state,
            controller_temp=data.get("controller_temperature"),
            load_voltage=data.get("load_voltage", 0.0),
            load_current=data.get("load_current", 0.0),
            load_power=data.get("load_power", 0.0),
            load_enabled=data.get("load_status", False),
            daily_energy=data.get("charging_amp_hours_today", 0.0) * data.get("battery_voltage", 12.0),
            total_energy=data.get("total_charging_amp_hours", 0.0) * data.get("battery_voltage", 12.0) / 1000.0,
            raw_data=data,
        )

    async def poll_continuous(
        self,
        callback: Callable[[RoverReading], None],
        interval: float = 30.0,
        stop_event: asyncio.Event = None,
    ):
        """Poll device continuously at interval."""
        while True:
            if stop_event and stop_event.is_set():
                break

            try:
                reading = await self.read_once(timeout=20.0)
                if reading:
                    self._last_reading = reading
                    callback(reading)
            except Exception as e:
                pass  # Log error but continue

            # Wait for interval or stop
            if stop_event:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    break  # Stop event was set
                except asyncio.TimeoutError:
                    pass  # Continue to next poll
            else:
                await asyncio.sleep(interval)
