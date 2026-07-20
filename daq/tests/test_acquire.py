#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from fakes import FakeSCA3300Device, patch_spidev

from sca3300 import SCA3300, RS_ERROR
from acquire import Acquirer, make_sca3300_read_fn


def base_config(**overrides) -> dict:
    cfg = {
        "spi": {"bus": 0, "device": 0, "max_speed_hz": 2_000_000, "mode": 1},
        "sampling": {"rate_hz": 2000, "block_size": 20, "queue_maxsize": 4},
        "realtime": {"use_sched_fifo": False, "priority": 80, "cpu_core": None},
        "logging": {"write_blocks_to_disk": False, "raw_dir": "data/raw", "health_log_interval_s": 5},
    }
    for section, values in overrides.items():
        cfg.setdefault(section, {}).update(values)
    return cfg


class TestMakeSca3300ReadFn(unittest.TestCase):
    def _started(self, device):
        patcher = patch_spidev(device)
        patcher.start()
        self.addCleanup(patcher.stop)
        sca = SCA3300()
        sca.start_up(status_reads=1, post_reset_delay_s=0, post_mode_delay_s=0)
        return sca

    def test_valid_reading_passes_through(self):
        device = FakeSCA3300Device()
        device.true_g = {"x": 0.0, "y": 0.0, "z": 1.0}
        sca = self._started(device)
        read_fn = make_sca3300_read_fn(sca)

        (x, y, z), valid = read_fn()
        self.assertTrue(valid)
        self.assertAlmostEqual(z, 1.0, delta=0.001)

    def test_rs_error_marks_invalid_and_triggers_reinit(self):
        device = FakeSCA3300Device()
        sca = self._started(device)
        device.rs = RS_ERROR
        read_fn = make_sca3300_read_fn(sca)

        _, valid = read_fn()
        self.assertFalse(valid)
        # reinit() re-runs start_up(), which reads WHOAMI again -- device
        # call_count should have advanced well past a single read_accel.
        self.assertGreater(device.call_count, 6)


class TestAcquirer(unittest.TestCase):
    def test_start_stop_and_block_shape(self):
        device = FakeSCA3300Device()
        device.true_g = {"x": 0.01, "y": 0.02, "z": 0.97}
        patcher = patch_spidev(device)
        patcher.start()
        self.addCleanup(patcher.stop)

        acquirer = Acquirer(base_config())
        acquirer.start()
        try:
            block = acquirer.get_block(timeout=5.0)
        finally:
            acquirer.stop()

        self.assertIsNotNone(block)
        self.assertEqual(block.samples.shape, (20, 3))
        np.testing.assert_allclose(block.samples[:, 2], 0.97, atol=0.01)

        health = acquirer.health_status()
        self.assertIn("samples_total", health)
        self.assertGreaterEqual(health["samples_total"], 20)

    def test_writes_block_to_disk_when_enabled(self):
        device = FakeSCA3300Device()
        patcher = patch_spidev(device)
        patcher.start()
        self.addCleanup(patcher.stop)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = base_config(logging={"write_blocks_to_disk": True, "raw_dir": tmp, "health_log_interval_s": 5})
            acquirer = Acquirer(cfg)
            acquirer.start()
            try:
                block = acquirer.get_block(timeout=5.0)
            finally:
                acquirer.stop()

            self.assertIsNotNone(block)
            written = list(Path(tmp).glob("block_*.npz"))
            self.assertEqual(len(written), 1)
            data = np.load(written[0])
            self.assertEqual(data["samples"].shape, (20, 3))


if __name__ == "__main__":
    unittest.main()
