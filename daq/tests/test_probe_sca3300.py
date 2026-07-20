#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeSCA3300Device, patch_spidev

from sca3300 import SCA3300, RS_ERROR
from probe_sca3300 import gravity_check, crc_burst_check, timing_characterization


class TestProbeFunctions(unittest.TestCase):
    def _make_started_sca(self, device=None):
        device = device or FakeSCA3300Device()
        patcher = patch_spidev(device)
        patcher.start()
        self.addCleanup(patcher.stop)
        sca = SCA3300()
        sca.start_up(status_reads=1, post_reset_delay_s=0, post_mode_delay_s=0)
        return sca, device

    def test_gravity_check_passes_when_one_axis_is_vertical(self):
        device = FakeSCA3300Device()
        device.true_g = {"x": 0.01, "y": -0.02, "z": 0.98}
        sca, _ = self._make_started_sca(device)

        result = gravity_check(sca, n_samples=20, settle_s=0.0)
        self.assertTrue(result["passed"])
        self.assertEqual(result["axis_reading_gravity"], "z")

    def test_gravity_check_fails_when_no_axis_near_1g(self):
        device = FakeSCA3300Device()
        device.true_g = {"x": 0.05, "y": 0.05, "z": 0.05}  # sensor in freefall / miswired
        sca, _ = self._make_started_sca(device)

        result = gravity_check(sca, n_samples=10, settle_s=0.0)
        self.assertFalse(result["passed"])
        self.assertIsNone(result["axis_reading_gravity"])

    def test_crc_burst_check_reports_full_pass_rate_on_clean_link(self):
        sca, _ = self._make_started_sca()
        result = crc_burst_check(sca, n_frames=50)
        self.assertEqual(result["crc_pass_rate"], 1.0)
        self.assertEqual(result["rs_error_count"], 0)

    def test_crc_burst_check_reports_rs_errors(self):
        device = FakeSCA3300Device()
        sca, _ = self._make_started_sca(device)
        device.rs = RS_ERROR
        result = crc_burst_check(sca, n_frames=20)
        self.assertEqual(result["rs_error_count"], 20)

    def test_timing_characterization_reports_expected_shape(self):
        sca, _ = self._make_started_sca()
        result = timing_characterization(sca, duration_s=0.05)
        self.assertGreater(result["count"], 0)
        self.assertIn("interval_us", result)
        for key in ("mean", "std", "min", "max", "p99"):
            self.assertIn(key, result["interval_us"])
        self.assertEqual(result["crc_pass_rate"], 1.0)

    def test_timing_characterization_holds_roughly_target_rate(self):
        sca, _ = self._make_started_sca()
        result = timing_characterization(sca, duration_s=0.2)
        # With a near-instant fake device, the loop is timer-bound, so mean
        # interval should track the 500us target reasonably closely.
        self.assertAlmostEqual(result["interval_us"]["mean"], 500.0, delta=100.0)


if __name__ == "__main__":
    unittest.main()
