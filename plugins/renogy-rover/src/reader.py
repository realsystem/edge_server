"""Read data from Renogy Rover MPPT via BLE (BT-2 module).

Direct BLE implementation using bleak - no external renogy library needed.
"""

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Callable, Optional

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
    battery_current: float  # A (charging current from PV)
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
    """Parse Modbus response, return register data or None on error.

    Rejects exception responses (function code with the high bit set) and
    frames whose CRC does not match, instead of returning garbage registers.
    """
    if len(data) < 5:
        return None
    # data[0] = device_id, data[1] = function, data[2] = byte_count
    if data[1] & 0x80:
        return None
    byte_count = data[2]
    frame_len = 3 + byte_count + 2
    if len(data) < frame_len:
        return None
    (crc,) = struct.unpack("<H", data[3 + byte_count : frame_len])
    if crc != _crc16(data[: 3 + byte_count]):
        return None
    return data[3 : 3 + byte_count]


def _parse_temp(raw: int) -> int:
    """Decode a Renogy temperature byte: bit 7 = sign (1 = negative), bits 0-6 = value."""
    value = raw & 0x7F
    return -value if raw & 0x80 else value


def parse_registers(data: bytes) -> RoverReading:
    """Decode the register block starting at 0x0100 into a RoverReading.

    Renogy Rover/Wanderer/Adventurer register map (offset from 0x0100):
       0  0x0100  Battery SOC (%)
       1  0x0101  Battery voltage x 0.1 V
       2  0x0102  Charging current x 0.01 A
       3  0x0103  Controller temp (high byte) / battery temp (low byte)
       4  0x0104  Load voltage x 0.1 V
       5  0x0105  Load current x 0.01 A
       6  0x0106  Load power (W)
       7  0x0107  PV voltage x 0.1 V
       8  0x0108  PV current x 0.01 A
       9  0x0109  Charging power (W)
      17  0x0111  Charging amp-hours today (Ah)
      19  0x0113  Power generation today (Wh)
      28  0x011C  Total power generation (kWh, 32-bit, regs 28-29)
      32  0x0120  Load status (high byte, bit 7) / charging state (low byte)
    """
    if len(data) < 20:
        raise ValueError(f"Need at least 10 registers, got {len(data)} bytes")

    def reg(offset: int) -> int:
        return struct.unpack(">H", data[offset * 2 : offset * 2 + 2])[0]

    def has(offset: int) -> bool:
        return len(data) >= offset * 2 + 2

    battery_soc = reg(0)
    battery_voltage = reg(1) / 10.0
    charging_current = reg(2) / 100.0

    temps = reg(3)
    controller_temp = _parse_temp(temps >> 8)
    battery_temp = _parse_temp(temps & 0xFF)

    load_voltage = reg(4) / 10.0
    load_current = reg(5) / 100.0
    load_power = float(reg(6))

    pv_voltage = reg(7) / 10.0
    pv_current = reg(8) / 100.0
    pv_power = float(reg(9))

    charging_ah_today = reg(17) if has(17) else 0
    daily_energy = float(reg(19)) if has(19) else 0.0
    total_energy = float((reg(28) << 16) | reg(29)) if has(29) else 0.0

    charge_state_code = 0
    load_enabled = False
    if has(32):
        status = reg(32)
        charge_state_code = status & 0xFF
        load_enabled = bool((status >> 8) & 0x80)
    charge_state = CHARGE_STATES.get(charge_state_code, f"state_{charge_state_code}")

    raw = {
        "battery_soc": battery_soc,
        "battery_voltage": battery_voltage,
        "charging_current": charging_current,
        "controller_temp": controller_temp,
        "battery_temp": battery_temp,
        "load_voltage": load_voltage,
        "load_current": load_current,
        "load_power": load_power,
        "pv_voltage": pv_voltage,
        "pv_current": pv_current,
        "pv_power": pv_power,
        "charge_state_code": charge_state_code,
        "charging_ah_today": charging_ah_today,
        "daily_energy_wh": daily_energy,
        "total_energy_kwh": total_energy,
    }

    return RoverReading(
        battery_voltage=battery_voltage,
        battery_current=charging_current,
        battery_soc=battery_soc,
        battery_temp=battery_temp,
        pv_voltage=pv_voltage,
        pv_current=pv_current,
        pv_power=pv_power,
        charge_state=charge_state,
        controller_temp=controller_temp,
        load_voltage=load_voltage,
        load_current=load_current,
        load_power=load_power,
        load_enabled=load_enabled,
        daily_energy=daily_energy,
        total_energy=total_energy,
        raw_data=raw,
    )


class RenogyReader:
    """Read data from Renogy Rover via BLE."""

    def __init__(self, address: str, device_id: int = 255):
        if not HAS_BLEAK:
            raise RuntimeError("bleak not installed (pip install bleak)")

        self.address = address.upper()
        self.device_id = device_id
        self._last_reading: Optional[RoverReading] = None
        self._response_data = bytearray()
        # Created per request inside the running loop: an Event made here would
        # be bound to whichever loop first used it and break on later calls.
        self._response_event: Optional[asyncio.Event] = None

    def _notification_handler(self, sender, data: bytes):
        """Handle BLE notifications."""
        self._response_data.extend(data)
        # Check if we have a complete response (or a 5-byte exception frame)
        if len(self._response_data) >= 5 and self._response_event is not None:
            if self._response_data[1] & 0x80:
                self._response_event.set()
                return
            expected_len = 3 + self._response_data[2] + 2
            if len(self._response_data) >= expected_len:
                self._response_event.set()

    async def _read_registers(self, client, start_reg: int, count: int) -> Optional[bytes]:
        """Read Modbus registers via BLE."""
        self._response_data.clear()
        self._response_event = asyncio.Event()

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

                try:
                    await client.stop_notify(NOTIFY_CHAR_UUID)
                except Exception:  # pragma: no cover - best effort
                    pass

                if not data or len(data) < 70:
                    log.warning(f"Incomplete data received: {len(data) if data else 0} bytes")
                    return None

                return self._parse_reading(data)

        except asyncio.TimeoutError:
            log.warning("Connection timeout")
            return None
        except Exception as e:
            log.error(f"BLE error: {e}")
            return None

    def read_once_sync(self, timeout: float = 30.0) -> Optional[RoverReading]:
        """Synchronous version of read_once."""
        return asyncio.run(self.read_once(timeout))

    def _parse_reading(self, data: bytes) -> RoverReading:
        """Parse register data into RoverReading."""
        return parse_registers(data)

    async def poll_continuous(
        self,
        callback: Callable[[RoverReading], None],
        interval: float = 30.0,
        stop_event: Optional[asyncio.Event] = None,
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
