#!/usr/bin/env python3
"""
Real-hardware acceptance tests for the SCA3300 link. Reuses the exact
functions probe_sca3300.py already exercises (and that tests/test_probe_sca3300.py
already covers against a fake device) -- the only thing new here is
asserting pass/fail against a REAL SCA3300 instead of printing a report.

Run with (see HARDWARE_TESTING.md for full details):
    DAQ_RUN_HARDWARE_TESTS=1 python3 -m unittest tests.hardware.test_sca3300_hardware -v
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hw_common import require_hardware_tests, load_config, DAQ_DIR

from sca3300 import SCA3300, SCA3300Error
from probe_sca3300 import gravity_check, crc_burst_check, timing_characterization, find_sensor_config


class TestSCA3300Hardware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_hardware_tests()

        cfg = load_config()
        sensor_cfg = find_sensor_config(cfg, os.environ.get("DAQ_SENSOR_NAME"))
        spi_cfg = sensor_cfg.get("spi", {})
        bus = int(os.environ.get("DAQ_SPI_BUS", spi_cfg.get("bus", 0)))
        device = int(os.environ.get("DAQ_SPI_DEVICE", spi_cfg.get("device", 0)))

        spidev_path = Path(f"/dev/spidev{bus}.{device}")
        if not spidev_path.exists():
            raise unittest.SkipTest(
                f"{spidev_path} not found -- is SPI enabled and the sensor wired to bus={bus} device={device}?"
            )

        cls.timing_duration_s = float(os.environ.get("DAQ_TIMING_DURATION_S", "10"))
        cls.sca = SCA3300(bus=bus, device=device,
                           max_speed_hz=int(os.environ.get("DAQ_SPI_SPEED", spi_cfg.get("max_speed_hz", 2_000_000))),
                           mode=int(os.environ.get("DAQ_SPI_MODE", spi_cfg.get("mode", 1))))
        cls.sca.start_up()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "sca"):
            cls.sca.close()

    def test_whoami_and_status_clean(self):
        self.assertEqual(self.sca.read_who_am_i(), 0x51)
        status = self.sca.read_status()
        self.assertTrue(status.clean, f"STATUS not clean: raw=0x{status.raw_value:03X} rs={status.rs:02b}")

    def test_crc_pass_rate_is_effectively_100_percent(self):
        result = crc_burst_check(self.sca, n_frames=2000)
        self.assertGreaterEqual(
            result["crc_pass_rate"], 0.999,
            f"CRC pass rate only {result['crc_pass_rate']*100:.2f}% -- check wiring, SPI mode, and clock speed"
        )
        self.assertEqual(result["rs_error_count"], 0, "RS reported an error during the CRC burst")

    def test_gravity_check_passes(self):
        """Board must be held still (any stable orientation) while this runs."""
        result = gravity_check(self.sca, n_samples=200)
        self.assertTrue(
            result["passed"],
            f"gravity check failed: mean_g={result['mean_g']} -- is the board actually still and wired correctly?"
        )

    def test_holds_2khz_cadence_within_acceptance_bar(self):
        """The Task 2 acceptance criterion from the original brief: ~0
        missed samples, p99 within +-5% of 500us. Default duration is 10s
        for a reasonably quick CI-adjacent run; override with
        DAQ_TIMING_DURATION_S=60 for the full acceptance-length check."""
        result = timing_characterization(self.sca, duration_s=self.timing_duration_s)
        interval = result["interval_us"]
        target = result["target_interval_us"]

        self.assertEqual(
            result["missed_count"], 0,
            f"{result['missed_count']} of {result['count']} intervals ({result['missed_pct']:.2f}%) "
            f"fell outside +-5% of {target}us -- if this persists, see README's MCU front-end fallback"
        )
        self.assertLessEqual(
            abs(interval["p99"] - target), 0.05 * target,
            f"p99={interval['p99']:.1f}us vs target {target}us +-5%"
        )


if __name__ == "__main__":
    unittest.main()
