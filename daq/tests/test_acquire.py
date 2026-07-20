#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from fakes import FakeSCA3300Device, patch_spidev

from sca3300 import SCA3300, RS_ERROR
from acquire import Acquirer, make_sca3300_read_fn


def sensor_entry(name="vibration_main", **overrides) -> dict:
    entry = {
        "name": name,
        "type": "sca3300",
        "spi": {"bus": 0, "device": 0, "max_speed_hz": 2_000_000, "mode": 1},
        "sampling": {"rate_hz": 2000, "block_size": 20, "queue_maxsize": 4},
        "realtime": {"use_sched_fifo": False, "required": False, "priority": 80,
                     "cpu_core": None, "spin_margin_us": 100},
    }
    entry.update(overrides)
    return entry


def base_config(sensors=None, **overrides) -> dict:
    cfg = {
        "sensors": sensors if sensors is not None else [sensor_entry()],
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


class TestAcquirerSingleSensor(unittest.TestCase):
    def test_start_stop_and_block_shape(self):
        device = FakeSCA3300Device()
        device.true_g = {"x": 0.01, "y": 0.02, "z": 0.97}
        patcher = patch_spidev(device)
        patcher.start()
        self.addCleanup(patcher.stop)

        acquirer = Acquirer(base_config())
        acquirer.start()
        try:
            # Single-sensor convenience: no name needed.
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
            written = list(Path(tmp).glob("vibration_main_*.npz"))
            self.assertEqual(len(written), 1)
            data = np.load(written[0])
            self.assertEqual(data["samples"].shape, (20, 3))

    def test_empty_sensors_list_rejected(self):
        with self.assertRaises(ValueError):
            Acquirer(base_config(sensors=[]))

    def test_unsupported_sensor_type_rejected(self):
        with self.assertRaises(ValueError):
            Acquirer(base_config(sensors=[sensor_entry(type="some_future_imu")]))


class TestAcquirerMultiSensor(unittest.TestCase):
    """This is the concrete, config-driven proof of the caveat fix: two
    sensors registered purely from config.yaml-shaped dicts, run
    concurrently, independently, and share one clock -- no Python code
    beyond config was needed to add the second one."""

    def test_two_sca3300_sensors_run_concurrently_from_config_alone(self):
        device_a = FakeSCA3300Device()
        device_a.true_g = {"x": 0.0, "y": 0.0, "z": 1.0}
        device_b = FakeSCA3300Device()
        device_b.true_g = {"x": 0.0, "y": 1.0, "z": 0.0}

        devices_by_bus_device = {(0, 0): device_a, (0, 1): device_b}

        def fake_spidev_factory():
            return _RoutingFakeSpiDev(devices_by_bus_device)

        with mock.patch("sca3300.spidev.SpiDev", fake_spidev_factory):
            cfg = base_config(sensors=[
                sensor_entry("vibration_main", spi={"bus": 0, "device": 0, "max_speed_hz": 2_000_000, "mode": 1}),
                sensor_entry("vibration_aux", spi={"bus": 0, "device": 1, "max_speed_hz": 2_000_000, "mode": 1}),
            ])
            acquirer = Acquirer(cfg)
            self.assertEqual(set(acquirer.sensor_names()), {"vibration_main", "vibration_aux"})

            acquirer.start()
            try:
                block_main = acquirer.get_block("vibration_main", timeout=5.0)
                block_aux = acquirer.get_block("vibration_aux", timeout=5.0)
            finally:
                acquirer.stop()

        self.assertIsNotNone(block_main)
        self.assertIsNotNone(block_aux)
        np.testing.assert_allclose(block_main.samples[:, 2], 1.0, atol=0.01)  # main: gravity on Z
        np.testing.assert_allclose(block_aux.samples[:, 1], 1.0, atol=0.01)   # aux: gravity on Y

        health = acquirer.health_status()  # >1 sensor -> aggregate dict
        self.assertEqual(set(health.keys()), {"vibration_main", "vibration_aux"})


class _RoutingFakeSpiDev:
    """Routes SCA3300's spidev.SpiDev() construction to a distinct
    FakeSCA3300Device per (bus, device), so a test can simulate two
    physically different SCA3300 units without real hardware."""

    def __init__(self, devices_by_bus_device: dict):
        self._devices = devices_by_bus_device
        self._device = None
        self.max_speed_hz = None
        self.mode = None

    def open(self, bus, dev):
        self._device = self._devices[(bus, dev)]

    def close(self):
        pass

    def xfer2(self, data):
        return list(self._device.request(bytes(data)))


if __name__ == "__main__":
    unittest.main()
