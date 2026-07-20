#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeSCA3300Device, patch_spidev

from sca3300 import (
    SCA3300, SCA3300CRCError, SCA3300StartupError,
    build_frame, crc8, parse_response, StatusFlags,
    REG_ACC_X, REG_ACC_Y, REG_ACC_Z, REG_STATUS, REG_MODE, REG_WHOAMI,
    MODE_SW_RESET_DATA, MODE_SELECT_DATA, RS_ERROR,
)


class TestCrcAndFrames(unittest.TestCase):
    """Every one of these is a literal frame independently cross-referenced
    against src/vibration_monitor.py (already tested on real hardware),
    Murata's upstream Linux IIO driver, and the algebratech/sca3300-driver
    community reference -- see README.md "Datasheet assumptions"."""

    KNOWN_GOOD_FRAMES = {
        "SW_RESET": (REG_MODE, True, MODE_SW_RESET_DATA, bytes([0xB4, 0x00, 0x20, 0x98])),
        "CHANGE_MODE1": (REG_MODE, True, MODE_SELECT_DATA[1], bytes([0xB4, 0x00, 0x00, 0x1F])),
        "READ_STATUS": (REG_STATUS, False, 0, bytes([0x18, 0x00, 0x00, 0xE5])),
        "READ_ACC_X": (REG_ACC_X, False, 0, bytes([0x04, 0x00, 0x00, 0xF7])),
        "READ_ACC_Y": (REG_ACC_Y, False, 0, bytes([0x08, 0x00, 0x00, 0xFD])),
        "READ_ACC_Z": (REG_ACC_Z, False, 0, bytes([0x0C, 0x00, 0x00, 0xFB])),
        "WHO_AM_I": (REG_WHOAMI, False, 0, bytes([0x40, 0x00, 0x00, 0x91])),
    }

    def test_build_frame_matches_known_good_bytes(self):
        for name, (reg, write, data, expected) in self.KNOWN_GOOD_FRAMES.items():
            with self.subTest(frame=name):
                self.assertEqual(build_frame(reg, write=write, data=data), expected)

    def test_crc8_detects_single_bit_corruption(self):
        frame = bytearray(build_frame(REG_ACC_X))
        frame[1] ^= 0x01  # flip one data bit
        self.assertNotEqual(crc8(bytes(frame[:3])), frame[3])

    def test_parse_response_rs_and_crc(self):
        raw = bytes([0b01, 0x0A, 0xBC, 0x00])
        result = parse_response(bytes(raw[:3]) + bytes([crc8(raw[:3])]))
        self.assertEqual(result.rs, 0b01)
        self.assertTrue(result.crc_ok)
        self.assertEqual(result.data16, 0x0ABC)

    def test_parse_response_signed16_negative(self):
        result = parse_response(build_frame(0, data=0xFFFF))
        self.assertEqual(result.signed16, -1)


class TestStatusFlags(unittest.TestCase):
    def test_clean_when_zero_and_rs_ok(self):
        self.assertTrue(StatusFlags(raw_value=0, rs=0b01).clean)

    def test_not_clean_when_bits_set(self):
        self.assertFalse(StatusFlags(raw_value=0x01, rs=0b01).clean)

    def test_not_clean_when_rs_error(self):
        self.assertFalse(StatusFlags(raw_value=0, rs=RS_ERROR).clean)


class TestSCA3300WithFakeHardware(unittest.TestCase):
    def _make_sca(self, device=None, mode=1):
        device = device or FakeSCA3300Device()
        patcher = patch_spidev(device)
        patcher.start()
        self.addCleanup(patcher.stop)
        return SCA3300(mode=mode), device

    def test_start_up_success(self):
        sca, device = self._make_sca()
        status = sca.start_up(status_reads=2, post_reset_delay_s=0, post_mode_delay_s=0)
        self.assertTrue(status.clean)
        self.assertEqual(sca.read_who_am_i(), 0x51)

    def test_start_up_raises_on_whoami_mismatch(self):
        device = FakeSCA3300Device()
        device.whoami_override = 0x00
        sca, _ = self._make_sca(device)
        with self.assertRaises(SCA3300StartupError):
            sca.start_up(status_reads=1, post_reset_delay_s=0, post_mode_delay_s=0)

    def test_start_up_raises_on_persistent_rs_error(self):
        device = FakeSCA3300Device()
        device.rs = RS_ERROR
        sca, _ = self._make_sca(device)
        with self.assertRaises(SCA3300StartupError):
            sca.start_up(status_reads=1, post_reset_delay_s=0, post_mode_delay_s=0)

    def test_read_accel_matches_simulated_gravity(self):
        device = FakeSCA3300Device()
        device.true_g = {"x": 0.02, "y": -0.01, "z": 0.99}
        sca, _ = self._make_sca(device)
        sca.start_up(status_reads=1, post_reset_delay_s=0, post_mode_delay_s=0)

        (x, y, z), rs, crc_ok = sca.read_accel()
        self.assertTrue(crc_ok)
        self.assertNotEqual(rs, RS_ERROR)
        self.assertAlmostEqual(x, 0.02, delta=0.001)
        self.assertAlmostEqual(y, -0.01, delta=0.001)
        self.assertAlmostEqual(z, 0.99, delta=0.001)

    def test_read_accel_reflects_mode_sensitivity(self):
        device = FakeSCA3300Device()
        device.true_g = {"x": 0.0, "y": 0.0, "z": 1.0}
        sca, _ = self._make_sca(device, mode=3)
        sca.start_up(status_reads=1, post_reset_delay_s=0, post_mode_delay_s=0)
        (_, _, z), _, crc_ok = sca.read_accel()
        self.assertTrue(crc_ok)
        self.assertAlmostEqual(z, 1.0, delta=0.001)

    def test_crc_error_is_detected_and_raised(self):
        device = FakeSCA3300Device()
        sca, _ = self._make_sca(device)
        sca.start_up(status_reads=1, post_reset_delay_s=0, post_mode_delay_s=0)

        # The NOP transfer immediately after the request is the one whose
        # reply actually carries the requested register's data (pipelining
        # lag) -- corrupt exactly that reply.
        base = device.call_count
        device.corrupt_at_call_index = base + 1
        with self.assertRaises(SCA3300CRCError):
            sca.read_axis_g(REG_ACC_X)

    def test_reinit_after_crc_error_recovers(self):
        device = FakeSCA3300Device()
        sca, _ = self._make_sca(device)
        sca.start_up(status_reads=1, post_reset_delay_s=0, post_mode_delay_s=0)

        base = device.call_count
        device.corrupt_at_call_index = base + 1
        with self.assertRaises(SCA3300CRCError):
            sca.read_axis_g(REG_ACC_X)

        # Comms are otherwise healthy, so reinit (re-running start_up) should
        # succeed and subsequent reads should work again.
        sca.reinit()
        (x, y, z), rs, crc_ok = sca.read_accel()
        self.assertTrue(crc_ok)


if __name__ == "__main__":
    unittest.main()
