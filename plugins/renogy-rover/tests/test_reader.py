"""Tests for the Renogy Modbus/BLE reader."""

import asyncio
import struct

import pytest

from renogy_rover import reader as reader_mod
from renogy_rover.reader import (
    RenogyReader,
    _crc16,
    create_modbus_request,
    parse_modbus_response,
    parse_registers,
)


def regs_to_bytes(values):
    return b"".join(struct.pack(">H", v) for v in values)


def build_block(**overrides):
    """35 registers starting at 0x0100 with realistic values."""
    regs = [0] * 35
    regs[0] = 87  # SOC %
    regs[1] = 132  # 13.2 V
    regs[2] = 455  # 4.55 A charging
    regs[3] = (25 << 8) | (0x80 | 5)  # controller 25C, battery -5C
    regs[4] = 131  # load 13.1 V
    regs[5] = 120  # load 1.20 A
    regs[6] = 16  # load 16 W
    regs[7] = 184  # PV 18.4 V
    regs[8] = 330  # PV 3.30 A
    regs[9] = 61  # 61 W
    regs[17] = 12  # 12 Ah today
    regs[19] = 158  # 158 Wh today
    regs[28], regs[29] = 0x0001, 0x0002  # 65538 kWh total
    regs[32] = (0x80 << 8) | 2  # load on, MPPT
    for k, v in overrides.items():
        regs[int(k[1:])] = v
    return regs_to_bytes(regs)


def frame(payload: bytes, device_id=255, fn=3) -> bytes:
    body = bytes([device_id, fn, len(payload)]) + payload
    return body + struct.pack("<H", _crc16(body))


class TestModbus:
    def test_crc_known_vector(self):
        # Standard Modbus example: 01 03 00 00 00 0A -> CRC C5 CD (LE: 0xCDC5)
        assert _crc16(bytes.fromhex("01030000000A")) == 0xCDC5

    def test_create_request(self):
        req = create_modbus_request(255, 0x0100, 35)
        assert req[:6] == bytes([0xFF, 0x03, 0x01, 0x00, 0x00, 0x23])
        assert struct.unpack("<H", req[6:])[0] == _crc16(req[:6])

    def test_parse_response_ok(self):
        payload = build_block()
        assert parse_modbus_response(frame(payload)) == payload

    def test_parse_response_rejects_bad_crc(self):
        bad = bytearray(frame(build_block()))
        bad[-1] ^= 0xFF
        assert parse_modbus_response(bytes(bad)) is None

    def test_parse_response_rejects_exception_frame(self):
        body = bytes([255, 0x83, 0x02])
        assert parse_modbus_response(body + struct.pack("<H", _crc16(body))) is None

    @pytest.mark.parametrize("data", [b"", b"\x01\x03", b"\xff\x03\x46\x00\x01"])
    def test_parse_response_short(self, data):
        assert parse_modbus_response(data) is None


class TestParseRegisters:
    def test_register_map(self):
        r = parse_registers(build_block())
        assert r.battery_soc == 87
        assert r.battery_voltage == pytest.approx(13.2)
        assert r.battery_current == pytest.approx(4.55)
        assert r.controller_temp == 25
        assert r.battery_temp == -5
        assert r.load_voltage == pytest.approx(13.1)
        assert r.load_current == pytest.approx(1.2)
        assert r.load_power == 16
        assert r.pv_voltage == pytest.approx(18.4)
        assert r.pv_current == pytest.approx(3.3)
        assert r.pv_power == 61
        assert r.daily_energy == 158
        assert r.total_energy == 65538
        assert r.charge_state == "mppt"
        assert r.load_enabled is True
        assert r.raw_data["charging_ah_today"] == 12

    def test_load_bit_does_not_corrupt_charge_state(self):
        # Previously the whole register was used, giving "state_32770"
        r = parse_registers(build_block(r32=(0x80 << 8) | 5))
        assert r.charge_state == "float"

    def test_unknown_state_and_load_off(self):
        r = parse_registers(build_block(r32=9))
        assert r.charge_state == "state_9"
        assert r.load_enabled is False

    def test_zero_temperature_is_reported(self):
        r = parse_registers(build_block(r3=0))
        assert r.controller_temp == 0 and r.battery_temp == 0

    def test_short_block_uses_defaults(self):
        r = parse_registers(build_block()[:20])  # registers 0-9 only
        assert r.pv_power == 61
        assert r.daily_energy == 0.0
        assert r.charge_state == "deactivated"

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            parse_registers(b"\x00" * 10)


class FakeClient:
    """Minimal BleakClient stand-in that answers a Modbus request."""

    def __init__(self, response_chunks):
        self.response_chunks = response_chunks
        self.handler = None
        self.written = []

    def __call__(self, address, timeout=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def start_notify(self, uuid, handler):
        self.handler = handler

    async def stop_notify(self, uuid):
        pass

    async def write_gatt_char(self, uuid, data):
        self.written.append(bytes(data))
        for chunk in self.response_chunks:  # BLE delivers in pieces
            self.handler(None, chunk)


@pytest.fixture
def fake_bleak(monkeypatch):
    def install(chunks):
        client = FakeClient(chunks)
        monkeypatch.setattr(reader_mod, "HAS_BLEAK", True)
        monkeypatch.setattr(reader_mod, "BleakClient", client)
        return client

    return install


class TestRenogyReader:
    def test_requires_bleak(self, monkeypatch):
        monkeypatch.setattr(reader_mod, "HAS_BLEAK", False)
        with pytest.raises(RuntimeError):
            RenogyReader("aa:bb")

    def test_read_once_chunked(self, fake_bleak):
        data = frame(build_block())
        client = fake_bleak([data[:20], data[20:40], data[40:]])
        r = RenogyReader("aa:bb:cc:dd:ee:ff")
        assert r.address == "AA:BB:CC:DD:EE:FF"
        reading = asyncio.run(r.read_once(timeout=1))
        assert reading is not None and reading.pv_voltage == pytest.approx(18.4)
        assert client.written[0][:2] == bytes([0xFF, 0x03])

    def test_read_once_sync_twice(self, fake_bleak):
        # Regression: an Event created in __init__ broke the second loop
        fake_bleak([frame(build_block())])
        r = RenogyReader("aa")
        assert r.read_once_sync(timeout=1) is not None
        assert r.read_once_sync(timeout=1) is not None

    def test_read_once_bad_crc(self, fake_bleak):
        bad = bytearray(frame(build_block()))
        bad[-2] ^= 0x01
        fake_bleak([bytes(bad)])
        assert asyncio.run(RenogyReader("aa").read_once(timeout=1)) is None

    def test_read_once_exception_frame(self, fake_bleak):
        body = bytes([255, 0x83, 0x02])
        fake_bleak([body + struct.pack("<H", _crc16(body))])
        assert asyncio.run(RenogyReader("aa").read_once(timeout=1)) is None

    def test_read_once_ble_error(self, monkeypatch):
        class Boom:
            def __init__(self, *a, **k):
                raise OSError("adapter gone")

        monkeypatch.setattr(reader_mod, "HAS_BLEAK", True)
        monkeypatch.setattr(reader_mod, "BleakClient", Boom)
        assert asyncio.run(RenogyReader("aa").read_once(timeout=1)) is None

    def test_poll_continuous_stops(self, fake_bleak):
        fake_bleak([frame(build_block())])
        r = RenogyReader("aa")
        seen = []

        async def go():
            stop = asyncio.Event()

            def cb(reading):
                seen.append(reading)
                stop.set()

            await r.poll_continuous(cb, interval=5, stop_event=stop)

        asyncio.run(asyncio.wait_for(go(), timeout=5))
        assert len(seen) == 1
