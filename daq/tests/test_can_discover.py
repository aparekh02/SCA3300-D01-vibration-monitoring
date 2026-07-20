#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from can_discover import detect_adapter, analyze, write_can_map_todo
from j1939 import PGN_EEC1, PGN_EEC2


def make_j1939_id(pgn: int, source_address: int, priority: int = 3) -> int:
    pdu_format = (pgn >> 8) & 0xFF
    pdu_specific = pgn & 0xFF if pdu_format >= 240 else 0x00
    reserved_dp = (pgn >> 16) & 0x3
    return (priority << 26) | (reserved_dp << 24) | (pdu_format << 16) | (pdu_specific << 8) | source_address


class TestDetectAdapter(unittest.TestCase):
    def test_detects_native_socketcan_from_ip_output(self):
        with mock.patch("can_discover._run") as run_mock, \
             mock.patch("can_discover.Path.exists", return_value=True), \
             mock.patch("can_discover.Path.resolve") as resolve_mock:
            run_mock.side_effect = lambda cmd: (
                "3: can0: <NOARP,UP,LOWER_UP> ... link/can  gs_usb\n" if "link" in cmd else ""
            )
            resolve_mock.return_value = Path("/sys/bus/usb/drivers/gs_usb")
            info = detect_adapter("can0")
        self.assertEqual(info["kind"], "native_socketcan")
        self.assertTrue(info["kernel_timestamping_expected"])

    def test_detects_slcan_from_dmesg_when_no_driver_symlink(self):
        with mock.patch("can_discover._run") as run_mock, \
             mock.patch("can_discover.Path.exists", return_value=False):
            def fake_run(cmd):
                if "dmesg" in cmd:
                    return "slcan: attached slcan0 (ttyUSB0)\n"
                return ""
            run_mock.side_effect = fake_run
            info = detect_adapter("slcan0")
        self.assertEqual(info["kind"], "slcan")
        self.assertFalse(info["kernel_timestamping_expected"])

    def test_unknown_when_no_evidence_found(self):
        with mock.patch("can_discover._run", return_value=""), \
             mock.patch("can_discover.Path.exists", return_value=False):
            info = detect_adapter("mystery0")
        self.assertEqual(info["kind"], "unknown")
        self.assertFalse(info["kernel_timestamping_expected"])


class TestAnalyze(unittest.TestCase):
    def _eec1_payload(self, rpm: float, torque_pct: float) -> bytes:
        raw_rpm = int(rpm / 0.125)
        payload = bytearray(8)
        payload[2] = int(torque_pct + 125)
        payload[3] = raw_rpm & 0xFF
        payload[4] = (raw_rpm >> 8) & 0xFF
        return bytes(payload)

    def test_eec1_surfaces_rpm_and_torque_candidates(self):
        can_id = make_j1939_id(PGN_EEC1, source_address=0x00)
        seen = {
            can_id: {
                "count": 600, "is_extended": True,
                "timestamps": [i * 0.1 for i in range(600)],
                "payloads": [self._eec1_payload(1500.0, 40.0)],
            }
        }
        result = analyze(seen, duration_s=60.0)
        names = {c["name"] for c in result["candidates"]}
        self.assertIn("engine_speed", names)
        self.assertIn("engine_actual_torque_pct", names)
        rpm_candidate = next(c for c in result["candidates"] if c["name"] == "engine_speed")
        self.assertAlmostEqual(rpm_candidate["sample_decoded_value"], 1500.0)
        self.assertAlmostEqual(rpm_candidate["rate_hz"], 10.0)
        self.assertTrue(result["timestamps_all_monotonic"])

    def test_non_j1939_11bit_id_produces_no_candidates_but_is_tabled(self):
        seen = {
            0x123: {
                "count": 60, "is_extended": False,
                "timestamps": [i * 1.0 for i in range(60)],
                "payloads": [b"\x01\x02\x03\x04\x05\x06\x07\x08"],
            }
        }
        result = analyze(seen, duration_s=60.0)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["ids"]), 1)
        self.assertEqual(result["ids"][0]["can_id_hex"], "0x123")

    def test_non_monotonic_timestamps_flagged(self):
        can_id = make_j1939_id(PGN_EEC2, source_address=0x00)
        seen = {
            can_id: {
                "count": 3, "is_extended": True,
                "timestamps": [1.0, 0.5, 2.0],  # goes backwards
                "payloads": [bytes(8)],
            }
        }
        result = analyze(seen, duration_s=10.0)
        self.assertFalse(result["timestamps_all_monotonic"])


class TestWriteCanMapTodo(unittest.TestCase):
    def test_generated_schema_matches_can_reader_expectations(self):
        candidates = [{
            "name": "engine_speed", "pgn": PGN_EEC1, "spn": 190,
            "can_id_hex": "0xCF00400", "source_address": 0, "rate_hz": 10.0,
            "sample_decoded_value": 1500.0, "byte_offset": 3, "length_bytes": 2,
            "byte_order": "little", "resolution": 0.125, "offset": 0.0,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "can_map.todo.yaml"
            write_can_map_todo(candidates, path)
            doc = yaml.safe_load(path.read_text())

        sig = doc["signals"]["engine_speed"]
        for required_field in ("protocol", "pgn", "byte_offset", "length_bytes",
                                "byte_order", "resolution", "offset", "confirmed"):
            self.assertIn(required_field, sig)
        self.assertEqual(sig["confirmed"], False)
        self.assertEqual(sig["byte_offset"], 3)


if __name__ == "__main__":
    unittest.main()
