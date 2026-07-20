#!/usr/bin/env python3
"""
The real Task 2 acceptance test, run through the actual
Acquirer/SensorHub/RealTimeSampler path (not sca3300.py directly, unlike
test_sca3300_hardware.py's timing check): >=60s at 2kHz, ~0 missed
samples, p99 within +-5% of target, on real hardware.

Run with (see HARDWARE_TESTING.md for full details):
    DAQ_RUN_HARDWARE_TESTS=1 DAQ_RUN_SOAK_TEST=1 python3 -m unittest tests.hardware.test_acquire_soak -v

Gated behind a second env var (slow by design) so it doesn't fire just
because the quicker hardware tests were requested. Override the 60s
default with DAQ_SOAK_DURATION_S.
"""

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hw_common import require_hardware_tests, require_soak_test, load_config

from acquire import Acquirer


class TestAcquireSoak(unittest.TestCase):
    def test_sustained_2khz_meets_acceptance_criteria(self):
        require_hardware_tests()
        require_soak_test()

        cfg = load_config()
        duration_s = float(os.environ.get("DAQ_SOAK_DURATION_S", "60"))
        rate_by_sensor = {s["name"]: s["sampling"]["rate_hz"] for s in cfg["sensors"]}

        acquirer = Acquirer(cfg)
        acquirer.start()
        try:
            deadline = time.monotonic() + duration_s
            while time.monotonic() < deadline:
                for name in acquirer.sensor_names():
                    acquirer.get_block(name, timeout=0.5)
        finally:
            acquirer.stop()

        for name in acquirer.sensor_names():
            with self.subTest(sensor=name):
                health = acquirer.health_status(name)
                target_us = 1e6 / rate_by_sensor[name]

                print(f"\n[{name}] samples={health['samples_total']} "
                      f"mean={health['mean_us']:.1f}us p99={health['p99_us']:.1f}us "
                      f"missed={health['missed_count']} ({health['missed_pct']:.2f}%) "
                      f"crc_errors={health['invalid_samples']} "
                      f"sched_fifo_active={health['sched_fifo_active']} cpu_pinned={health['cpu_pinned']}")

                self.assertEqual(
                    health["missed_count"], 0,
                    f"[{name}] {health['missed_count']} missed samples ({health['missed_pct']:.2f}%) over "
                    f"{duration_s:.0f}s -- see README MCU front-end fallback if this doesn't clear up"
                )
                self.assertLessEqual(
                    abs(health["p99_us"] - target_us), 0.05 * target_us,
                    f"[{name}] p99={health['p99_us']:.1f}us vs target {target_us:.1f}us +-5%"
                )
                self.assertEqual(health["invalid_samples"], 0, f"[{name}] {health['invalid_samples']} CRC/RS errors")


if __name__ == "__main__":
    unittest.main()
