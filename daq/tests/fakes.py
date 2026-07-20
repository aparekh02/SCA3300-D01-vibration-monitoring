#!/usr/bin/env python3
"""
fakes.py - a small SCA3300 protocol simulator so sca3300.py, acquire.py, and
probe_sca3300.py can be unit-tested without real SPI hardware attached.

Deliberately re-implements the wire protocol independently of sca3300.py's
own crc8()/build_frame() (rather than importing them) so a bug in the
driver's frame handling doesn't silently pass a test that uses the same
buggy code to build its expected replies.
"""

from __future__ import annotations

from unittest import mock

REG_ACC_X = 0x01
REG_ACC_Y = 0x02
REG_ACC_Z = 0x03
REG_STATUS = 0x06
REG_MODE = 0x0D
REG_WHOAMI = 0x10

WHOAMI_ID = 0x51
MODE_SW_RESET_DATA = 0x0020

SENSITIVITY_BY_MODE = {1: 2700.0, 2: 1350.0, 3: 5400.0, 4: 5400.0}

_CRC8_POLY = 0x1D
_CRC8_INIT = 0xFF


def _crc8(data: bytes) -> int:
    crc = _CRC8_INIT
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ _CRC8_POLY) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return (~crc) & 0xFF


class FakeSCA3300Device:
    """Simulates the SCA3300's off-frame (pipelined) SPI protocol: the
    reply to request N arrives on request N+1, exactly like real hardware.
    """

    def __init__(self):
        self.current_mode = 1
        self.true_g = {"x": 0.0, "y": 0.0, "z": 1.0}
        self.status_raw = 0
        self.rs = 0b01  # matches this repo's observed post-startup value
        self.whoami_override = None  # set to simulate a WHOAMI mismatch
        self.call_count = 0
        self.corrupt_at_call_index = None  # 0-based index of the reply to corrupt

        self._pending_reg = None
        self._pending_data = None

    def request(self, frame: bytes) -> bytes:
        op = frame[0]
        write = bool(op & 0x80)
        reg = (op >> 2) & 0x1F
        data16 = (frame[1] << 8) | frame[2]

        idx = self.call_count
        self.call_count += 1

        reply = bytearray(self._build_reply(self._pending_reg))
        if self.corrupt_at_call_index == idx:
            reply[3] ^= 0xFF
            self.corrupt_at_call_index = None

        if write and reg == REG_MODE:
            if data16 == MODE_SW_RESET_DATA:
                self.status_raw = 0
            elif data16 in (0, 1, 2, 3):
                self.current_mode = data16 + 1

        self._pending_reg = reg
        self._pending_data = data16
        return bytes(reply)

    def _build_reply(self, reg) -> bytes:
        if reg == REG_WHOAMI:
            data_out = self.whoami_override if self.whoami_override is not None else WHOAMI_ID
        elif reg == REG_STATUS:
            data_out = self.status_raw
        elif reg == REG_ACC_X:
            data_out = self._encode_g(self.true_g["x"])
        elif reg == REG_ACC_Y:
            data_out = self._encode_g(self.true_g["y"])
        elif reg == REG_ACC_Z:
            data_out = self._encode_g(self.true_g["z"])
        else:
            data_out = 0

        payload = bytes([self.rs & 0x03, (data_out >> 8) & 0xFF, data_out & 0xFF])
        return payload + bytes([_crc8(payload)])

    def _encode_g(self, g_value: float) -> int:
        sensitivity = SENSITIVITY_BY_MODE[self.current_mode]
        raw = int(round(g_value * sensitivity)) & 0xFFFF
        return raw


class FakeSpiDev:
    def __init__(self, device: FakeSCA3300Device):
        self._device = device
        self.max_speed_hz = None
        self.mode = None

    def open(self, bus, dev):
        pass

    def close(self):
        pass

    def xfer2(self, data):
        return list(self._device.request(bytes(data)))


def patch_spidev(device: FakeSCA3300Device):
    """Returns a mock.patch context manager replacing sca3300.spidev.SpiDev
    with a fake bound to `device`, so an SCA3300 instance constructed
    inside the `with` block talks to the simulator instead of real
    hardware."""
    return mock.patch("sca3300.spidev.SpiDev", lambda: FakeSpiDev(device))
