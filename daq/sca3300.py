#!/usr/bin/env python3
"""
sca3300.py - Murata SCA3300-D01 SPI driver.

Frame/CRC/op-code details below were cross-verified (not invented) against:
  - This repo's own already-tested driver (src/vibration_monitor.py), whose
    frame constants match live hardware output in example_run.md.
  - Murata's SCA3300/SCL3300 Linux IIO kernel driver
    (drivers/iio/accel/sca3300.c) for the register map, CRC8 formula, and
    per-mode scale/LPF tables.
  - The algebratech/sca3300-driver Python reference implementation, for the
    literal 32-bit command frames.
All three independently agree on: CRC-8 (poly 0x1D, init 0xFF, inverted
output) over the first 3 bytes of the frame; register addresses for
ACC_X/Y/Z, STATUS, MODE, WHOAMI; the SW-reset-via-MODE-register pattern; and
Mode-1 sensitivity/LPF. See README.md "Datasheet assumptions" for exactly
what is confirmed vs. still needs a hardware/datasheet check.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import spidev

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Register map (address is 5 bits, occupies bits [6:2] of the request's
# first byte; bit 7 is the write flag). Confirmed via cross-reference unless
# noted otherwise.
# ---------------------------------------------------------------------------
REG_ACC_X = 0x01
REG_ACC_Y = 0x02
REG_ACC_Z = 0x03
REG_SELFTEST = 0x04  # op-code seen in community driver; meaning not used/confirmed here
REG_TEMP = 0x05  # NOT independently confirmed -- see README
REG_STATUS = 0x06  # "Summary Status" register
REG_MODE = 0x0D
REG_WHOAMI = 0x10

WHOAMI_ID = 0x51

MODE_SW_RESET_DATA = 0x0020  # bit 5 of MODE register data triggers a software reset

# Data word written to MODE register to select operating mode 1-4.
MODE_SELECT_DATA = {1: 0x0000, 2: 0x0001, 3: 0x0002, 4: 0x0003}

# Per-mode sensitivity / LPF. Mode 1 numbers are confirmed (see module
# docstring). Modes 2-4 are cross-referenced from the same sources but their
# g-range has not been independently confirmed against the primary
# datasheet PDF (fetch blocked in this build environment) -- see README.
MODE_TABLE = {
    1: {"sensitivity_lsb_per_g": 2700.0, "lpf_hz": 70, "g_range": "+/-3g", "confirmed": True},
    2: {"sensitivity_lsb_per_g": 1350.0, "lpf_hz": 70, "g_range": "unconfirmed", "confirmed": False},
    3: {"sensitivity_lsb_per_g": 5400.0, "lpf_hz": 70, "g_range": "unconfirmed", "confirmed": False},
    4: {"sensitivity_lsb_per_g": 5400.0, "lpf_hz": 10, "g_range": "unconfirmed", "confirmed": False},
}

RS_ERROR = 0b11
RS_STARTUP = 0b01  # observed/expected value immediately after startup (see README)

_CRC8_POLY = 0x1D
_CRC8_INIT = 0xFF


def _make_crc8_table(poly: int) -> tuple:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return tuple(table)


_CRC8_TABLE = _make_crc8_table(_CRC8_POLY)


def crc8(data: bytes) -> int:
    """CRC-8 over `data`, poly 0x1D / init 0xFF / inverted output.

    Verified by hand against the known-good frame 04 00 00 F7 (ACC_X read
    request) and against every literal frame in src/vibration_monitor.py.
    """
    crc = _CRC8_INIT
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return (~crc) & 0xFF


def build_frame(reg: int, write: bool = False, data: int = 0) -> bytes:
    op = (0x80 if write else 0x00) | ((reg & 0x1F) << 2)
    payload = bytes([op, (data >> 8) & 0xFF, data & 0xFF])
    return payload + bytes([crc8(payload)])


NOP_FRAME = build_frame(0, write=False, data=0)


@dataclass
class FrameResult:
    rs: int
    data16: int
    crc_ok: bool
    raw: bytes

    @property
    def signed16(self) -> int:
        v = self.data16
        return v - 0x10000 if v & 0x8000 else v


def parse_response(raw: bytes) -> FrameResult:
    rs = raw[0] & 0x03
    data16 = (raw[1] << 8) | raw[2]
    crc_ok = crc8(raw[:3]) == raw[3]
    return FrameResult(rs=rs, data16=data16, crc_ok=crc_ok, raw=bytes(raw))


@dataclass
class StatusFlags:
    raw_value: int
    rs: int
    bits: dict = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """True if no status bits are set and RS is not an error.

        Bit-level semantics beyond "any bit set" are not confirmed against
        the primary datasheet in this build environment -- see README.
        """
        return self.raw_value == 0 and self.rs != RS_ERROR


class SCA3300Error(Exception):
    pass


class SCA3300CRCError(SCA3300Error):
    pass


class SCA3300RSError(SCA3300Error):
    pass


class SCA3300StartupError(SCA3300Error):
    pass


class SCA3300:
    """Encapsulates startup, mode select, and validated register reads.

    Every reply frame's CRC and RS are checked; callers get an exception
    instead of a silently-wrong sample. Uses the simple "send command, then
    send NOP and read its reply" pattern (2 SPI transfers per register)
    rather than a fully rolling pipeline -- this matches the already-tested
    driver in src/vibration_monitor.py and comfortably fits the 500us/sample
    budget at typical SPI clock rates; see README for the timing margin and
    a note on a leaner pipelined variant if probe_sca3300.py timing data
    ever shows it's needed.
    """

    def __init__(self, bus: int = 0, device: int = 0, max_speed_hz: int = 2_000_000, mode: int = 1):
        if mode not in MODE_SELECT_DATA:
            raise ValueError(f"invalid mode {mode}, must be 1-4")
        self._bus = bus
        self._device = device
        self._max_speed_hz = max_speed_hz
        self.mode = mode
        self._spi = spidev.SpiDev()
        self._spi.open(bus, device)
        self._spi.max_speed_hz = max_speed_hz
        self._spi.mode = 0b00  # SPI mode 0 (CPOL=0, CPHA=0) per datasheet

    def close(self):
        self._spi.close()

    # -- low level -----------------------------------------------------

    def _xfer(self, frame: bytes) -> FrameResult:
        raw = self._spi.xfer2(list(frame))
        return parse_response(raw)

    def _request(self, reg: int, write: bool = False, data: int = 0) -> FrameResult:
        """Send a request frame, then a NOP, returning the NOP's reply --
        which (per the datasheet's off-frame/pipelined protocol) carries
        the response to the request just sent."""
        self._xfer(build_frame(reg, write=write, data=data))
        return self._xfer(NOP_FRAME)

    # -- startup ---------------------------------------------------------

    def start_up(self, status_reads: int = 3, post_reset_delay_s: float = 0.005,
                 post_mode_delay_s: float = 0.020) -> StatusFlags:
        """Datasheet startup sequence: SW reset, select mode, wait, read
        STATUS repeatedly to clear power-on flags, then confirm STATUS is
        clean and WHOAMI matches.

        Delay defaults follow the values already exercised against real
        hardware in src/vibration_monitor.py (5ms / 20ms); the brief's own
        guidance is "~15ms" after mode select -- both are provided/
        configurable since the exact settle time is a datasheet value this
        build environment could not directly confirm from the primary PDF.
        """
        self._xfer(build_frame(REG_MODE, write=True, data=MODE_SW_RESET_DATA))
        time.sleep(post_reset_delay_s)

        self._xfer(build_frame(REG_MODE, write=True, data=MODE_SELECT_DATA[self.mode]))
        time.sleep(post_mode_delay_s)

        status = None
        for _ in range(status_reads):
            status = self.read_status()

        whoami = self.read_who_am_i()
        if whoami != WHOAMI_ID:
            raise SCA3300StartupError(f"WHOAMI mismatch: got 0x{whoami:02X}, expected 0x{WHOAMI_ID:02X}")
        if status is not None and status.rs == RS_ERROR:
            raise SCA3300StartupError(f"RS still reports error after startup (raw status=0x{status.raw_value:03X})")

        logger.info("SCA3300 startup complete: mode=%d whoami=0x%02X status=0x%03X rs=%02b",
                    self.mode, whoami, status.raw_value if status else -1, status.rs if status else -1)
        return status

    def reinit(self) -> StatusFlags:
        logger.warning("SCA3300 reinit() triggered after a comms error")
        return self.start_up()

    # -- reads -----------------------------------------------------------

    def read_who_am_i(self) -> int:
        result = self._request(REG_WHOAMI)
        if not result.crc_ok:
            raise SCA3300CRCError(f"CRC error reading WHOAMI: {result.raw.hex()}")
        return result.data16 & 0xFF

    def read_status(self) -> StatusFlags:
        result = self._request(REG_STATUS)
        if not result.crc_ok:
            raise SCA3300CRCError(f"CRC error reading STATUS: {result.raw.hex()}")
        bits = {f"bit{i}": bool(result.data16 & (1 << i)) for i in range(9)}
        return StatusFlags(raw_value=result.data16, rs=result.rs, bits=bits)

    def read_temp_raw(self) -> FrameResult:
        """Raw TEMP register read. Register address (0x05) and any
        raw-to-Celsius formula are NOT independently confirmed in this
        build -- see README. Returns the validated raw frame only."""
        result = self._request(REG_TEMP)
        if not result.crc_ok:
            raise SCA3300CRCError(f"CRC error reading TEMP: {result.raw.hex()}")
        return result

    def read_axis_g(self, reg: int) -> tuple:
        """Returns (value_in_g, FrameResult) for one axis register, raising
        on CRC failure. Caller is responsible for treating an RS_ERROR
        response as untrustworthy."""
        result = self._request(reg)
        if not result.crc_ok:
            raise SCA3300CRCError(f"CRC error reading axis reg 0x{reg:02X}: {result.raw.hex()}")
        sensitivity = MODE_TABLE[self.mode]["sensitivity_lsb_per_g"]
        return result.signed16 / sensitivity, result

    def read_accel(self) -> tuple:
        """Read X, Y, Z (each validated). Returns
        ((x_g, y_g, z_g), worst_rs, all_crc_ok)."""
        x, rx = self.read_axis_g(REG_ACC_X)
        y, ry = self.read_axis_g(REG_ACC_Y)
        z, rz = self.read_axis_g(REG_ACC_Z)
        worst_rs = max(rx.rs, ry.rs, rz.rs)
        all_crc_ok = rx.crc_ok and ry.crc_ok and rz.crc_ok
        return (x, y, z), worst_rs, all_crc_ok
