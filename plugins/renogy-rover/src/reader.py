"""Read data from Renogy Rover MPPT via BLE (BT-2 module).

Uses cyrils/renogy-bt library from GitHub.
Install: pip install git+https://github.com/cyrils/renogy-bt.git
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Callable

try:
    from renogybt import RoverClient, RoverHistoryClient, BatteryClient
    HAS_RENOGYBT = True
except ImportError:
    HAS_RENOGYBT = False
    RoverClient = None

log = logging.getLogger(__name__)


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
    0: "deactivated",
    1: "activated",
    2: "mppt",
    3: "equalizing",
    4: "boost",
    5: "float",
    6: "current_limiting",
}


class RenogyReader:
    """Read data from Renogy Rover via BLE using renogybt."""

    def __init__(self, address: str, device_id: int = 255):
        if not HAS_RENOGYBT:
            raise RuntimeError(
                "renogybt not installed. Install with:\n"
                "  pip install git+https://github.com/cyrils/renogy-bt.git"
            )

        self.address = address.upper()
        self.device_id = device_id
        self._last_reading: Optional[RoverReading] = None

    def read_once_sync(self, timeout: float = 30.0) -> Optional[RoverReading]:
        """Read data from the device once (synchronous)."""
        import time

        result_data = {}
        done = False
        error_msg = None

        def on_data(client, data):
            nonlocal result_data, done
            result_data = data
            done = True

        def on_error(client, error):
            nonlocal error_msg, done
            error_msg = str(error)
            done = True

        config = {
            "device": {
                "adapter": "hci0",
                "mac_addr": self.address,
                "alias": "Rover",
                "device_id": self.device_id,
            },
            "data": {
                "enable": True,
            },
            "logging": {
                "enable": False,
            },
            "mqtt": {
                "enable": False,
            },
            "pvoutput": {
                "enable": False,
            },
            "remote_logging": {
                "enable": False,
            },
        }

        try:
            client = RoverClient(config)
            client.set_callback(on_data)
            client.set_error_callback(on_error)
            client.connect()

            start = time.time()
            while not done and (time.time() - start) < timeout:
                time.sleep(0.1)

            client.disconnect()

        except Exception as e:
            log.error(f"BLE connection error: {e}")
            return None

        if error_msg:
            log.error(f"Device error: {error_msg}")
            return None

        if not result_data:
            return None

        return self._parse_reading(result_data)

    async def read_once(self, timeout: float = 30.0) -> Optional[RoverReading]:
        """Read data from the device once (async wrapper)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.read_once_sync, timeout)

    def _parse_reading(self, data: dict) -> RoverReading:
        """Parse raw data into RoverReading."""
        # Charging state
        charge_state_code = data.get("charging_status", 0)
        if isinstance(charge_state_code, str):
            charge_state = charge_state_code
        else:
            charge_state = CHARGE_STATES.get(charge_state_code, f"unknown({charge_state_code})")

        # Get values with fallbacks for different field names
        battery_voltage = data.get("battery_voltage", 0.0)
        pv_voltage = data.get("pv_voltage", data.get("solar_voltage", 0.0))
        pv_current = data.get("pv_current", data.get("solar_current", 0.0))
        pv_power = data.get("pv_power", data.get("solar_power", pv_voltage * pv_current))

        # Daily energy
        daily_ah = data.get("charging_amp_hours_today", 0.0)
        daily_energy = daily_ah * battery_voltage if battery_voltage else 0.0

        # Total energy
        total_ah = data.get("total_charging_amp_hours", 0.0)
        total_energy = (total_ah * battery_voltage / 1000.0) if battery_voltage else 0.0

        return RoverReading(
            battery_voltage=battery_voltage,
            battery_current=data.get("charging_current", data.get("battery_current", 0.0)),
            battery_soc=data.get("battery_percentage", data.get("state_of_charge", 0)),
            battery_temp=data.get("battery_temperature"),
            pv_voltage=pv_voltage,
            pv_current=pv_current,
            pv_power=pv_power,
            charge_state=charge_state,
            controller_temp=data.get("controller_temperature"),
            load_voltage=data.get("load_voltage", 0.0),
            load_current=data.get("load_current", 0.0),
            load_power=data.get("load_power", 0.0),
            load_enabled=bool(data.get("load_status", False)),
            daily_energy=daily_energy,
            total_energy=total_energy,
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
                log.warning(f"Poll error: {e}")

            # Wait for interval or stop
            if stop_event:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)
