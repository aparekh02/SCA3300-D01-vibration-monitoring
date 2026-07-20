#!/usr/bin/env python3
"""
Real-hardware acceptance tests for the CAN adapter/bus. Reuses
can_discover.py's detect_adapter()/sniff_bus()/analyze() (already unit
tested against synthetic data in tests/test_can_discover.py) against the
real adapter and bus.

Run with (see HARDWARE_TESTING.md for full details):
    DAQ_RUN_HARDWARE_TESTS=1 python3 -m unittest tests.hardware.test_can_hardware -v

Requires actual live traffic on the bus during the sniff window (e.g. the
engine running, or a bench CAN simulator) -- a quiet bus will fail
test_bus_has_live_traffic_and_monotonic_timestamps by design, since "no
traffic" is exactly the kind of silent failure this test exists to catch.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hw_common import require_hardware_tests, load_config

import can

from can_discover import detect_adapter, sniff_bus, analyze


class TestCanHardware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_hardware_tests()
        cfg = load_config()
        can_cfg = cfg.get("can", {})
        cls.iface = os.environ.get("DAQ_CAN_IFACE", can_cfg.get("channel", "can0"))
        cls.bustype = os.environ.get("DAQ_CAN_BUSTYPE", can_cfg.get("bustype", "socketcan"))
        cls.bitrate = int(os.environ.get("DAQ_CAN_BITRATE", can_cfg.get("bitrate", 250000)))
        cls.duration_s = float(os.environ.get("DAQ_CAN_SNIFF_DURATION_S", "10"))

    def test_adapter_detection_runs_and_reports_a_kind(self):
        info = detect_adapter(self.iface)
        self.assertIn(info["kind"], ("native_socketcan", "slcan", "unknown"))
        if info["kind"] == "unknown":
            print(f"\nNOTE: adapter kind not auto-detected for {self.iface}; cross-check manually. "
                  f"Evidence gathered: {info['evidence']}")
        else:
            print(f"\n{self.iface}: kind={info['kind']} driver={info['driver']} "
                  f"kernel_timestamping_expected={info['kernel_timestamping_expected']}")

    def test_bus_has_live_traffic_and_monotonic_timestamps(self):
        try:
            seen = sniff_bus(self.iface, self.bustype, self.bitrate, self.duration_s)
        except (can.CanError, OSError) as exc:
            self.fail(f"could not open/sniff {self.iface} ({self.bustype}@{self.bitrate}): {exc}")

        self.assertGreater(
            len(seen), 0,
            f"no CAN traffic seen on {self.iface} in {self.duration_s}s -- "
            f"is the bus actually live (engine running / bench simulator active)?"
        )
        analysis = analyze(seen, self.duration_s)
        self.assertTrue(analysis["timestamps_all_monotonic"], "non-monotonic CAN timestamps -- check adapter/driver")

        if analysis["candidates"]:
            print("\nJ1939 candidates observed:")
            for c in analysis["candidates"]:
                print(f"  {c['name']} id={c['can_id_hex']} rate={c['rate_hz']:.2f}Hz value={c['sample_decoded_value']}")
        else:
            print("\nNo EEC1/EEC2 J1939 candidates in this window -- expected if the bus is proprietary; "
                  "edit can_map.todo.yaml manually from the observed ID table instead.")


if __name__ == "__main__":
    unittest.main()
