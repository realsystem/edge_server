"""Read data from Renogy Rover MPPT via BLE (BT-2 module).

Direct BLE implementation using bleak - no external renogy library needed.
"""

import asyncio
import struct
import logging
from dataclasses import dataclass
from typing import Optional, Callable

try:
    from bleak import BleakClient
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False
    BleakClient = None

log = logging.getLogger(__name__)

# Renogy BLE UUIDs
NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"


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
    charge_state: str = "unknown"
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


def create_modbus_request(device_id: int, start_reg: int, count: int) -> bytes:
    """Create a Modbus RTU read holding registers request."""
    # Function code 0x03 = Read Holding Registers
    data = struct.pack(">BBH", device_id, 0x03, start_reg)
    data += struct.pack(">H", count)
    crc = _crc16(data)
    return data + struct.pack("<H", crc)


def _crc16(data: bytes) -> int:
    """Calculate Modbus CRC16."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def parse_modbus_response(data: bytes) -> Optional[bytes]:
    """Parse Modbus response, return register data or None on error."""
    if len(data) < 5:
        return None
    # data[0] = device_id, data[1] = function, data[2] = byte_count
    byte_count = data[2]
    if len(data) < 3 + byte_count + 2:
        return None
    return data[3:3 + byte_count]


class RenogyReader:
    """Read data from Renogy Rover via BLE."""

    def __init__(self, address: str, device_id: int = 255):
        if not HAS_BLEAK:
            raise RuntimeError("bleak not installed (pip install bleak)")

        self.address = address.upper()
        self.device_id = device_id
        self._last_reading: Optional[RoverReading] = None
        self._response_data = bytearray()
        self._response_event = asyncio.Event()

    def _notification_handler(self, sender, data: bytes):
        """Handle BLE notifications."""
        self._response_data.extend(data)
        # Check if we have a complete response
        if len(self._response_data) >= 5:
            expected_len = 3 + self._response_data[2] + 2
            if len(self._response_data) >= expected_len:
                self._response_event.set()

    async def _read_registers(self, client: BleakClient, start_reg: int, count: int) -> Optional[bytes]:
        """Read Modbus registers via BLE."""
        self._response_data.clear()
        self._response_event.clear()

        request = create_modbus_request(self.device_id, start_reg, count)
        await client.write_gatt_char(WRITE_CHAR_UUID, request)

        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            return None

        return parse_modbus_response(bytes(self._response_data))

    async def read_once(self, timeout: float = 30.0) -> Optional[RoverReading]:
        """Read data from the device once."""
        try:
            async with BleakClient(self.address, timeout=timeout) as client:
                # Start notifications
                await client.start_notify(NOTIFY_CHAR_UUID, self._notification_handler)

                # Read main registers (0x0100 - 0x0122, 35 registers)
                # Contains: battery voltage, current, SOC, temps, PV data, load, etc.
                data = await self._read_registers(client, 0x0100, 35)

                if not data or len(data) < 70:
                    log.warning(f"Incomplete data received: {len(data) if data else 0} bytes")
                    return None

                await client.stop_notify(NOTIFY_CHAR_UUID)
                return self._parse_reading(data)

        except asyncio.TimeoutError:
            log.warning("Connection timeout")
            return None
        except Exception as e:
            log.error(f"BLE error: {e}")
            return None

    def read_once_sync(self, timeout: float = 30.0) -> Optional[RoverReading]:
        """Synchronous version of read_once."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(self.read_once(timeout))

    def _parse_reading(self, data: bytes) -> RoverReading:
        """Parse register data into RoverReading."""
        raw = {}

        # Parse 16-bit registers (big-endian)
        def reg(offset: int) -> int:
            return struct.unpack(">H", data[offset*2:offset*2+2])[0]

        def signed_reg(offset: int) -> int:
            return struct.unpack(">h", data[offset*2:offset*2+2])[0]

        # Battery (registers 0x0100+)
        battery_soc = reg(0)
        battery_voltage = reg(1) / 10.0
        battery_current = signed_reg(2) / 100.0

        # Temperatures (offset 3-4)
        # Renogy stores temps as: upper byte = sign (0=negative, 1=positive), lower byte = value
        def parse_temp(offset: int) -> int:
            val = reg(offset)
            sign = (val >> 8) & 0xFF
            temp = val & 0xFF
            return temp if sign else -temp

        controller_temp = parse_temp(3)
        battery_temp = parse_temp(4)

        # Load (offset 5-7)
        load_voltage = reg(5) / 10.0
        load_current = reg(6) / 100.0
        load_power = reg(7)

        # PV / Solar (offset 8-10)
        pv_voltage = reg(8) / 10.0
        pv_current = reg(9) / 100.0
        pv_power = reg(10)

        # Daily stats (offset 11-13)
        daily_ah = reg(13)
        daily_energy = daily_ah * battery_voltage

        # Charging state (offset 32)
        charge_state_code = reg(32) if len(data) >= 66 else 0
        charge_state = CHARGE_STATES.get(charge_state_code, f"state_{charge_state_code}")

        # Load status (offset 33)
        load_enabled = bool(reg(33)) if len(data) >= 68 else False

        raw = {
            "battery_soc": battery_soc,
            "battery_voltage": battery_voltage,
            "battery_current": battery_current,
            "controller_temp": controller_temp,
            "battery_temp": battery_temp,
            "load_voltage": load_voltage,
            "load_current": load_current,
            "load_power": load_power,
            "pv_voltage": pv_voltage,
            "pv_current": pv_current,
            "pv_power": pv_power,
            "charge_state_code": charge_state_code,
            "daily_ah": daily_ah,
        }

        return RoverReading(
            battery_voltage=battery_voltage,
            battery_current=battery_current,
            battery_soc=battery_soc,
            battery_temp=battery_temp if battery_temp != 0 else None,
            pv_voltage=pv_voltage,
            pv_current=pv_current,
            pv_power=pv_power,
            charge_state=charge_state,
            controller_temp=controller_temp if controller_temp != 0 else None,
            load_voltage=load_voltage,
            load_current=load_current,
            load_power=load_power,
            load_enabled=load_enabled,
            daily_energy=daily_energy,
            total_energy=0.0,  # Would need separate register read
            raw_data=raw,
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
