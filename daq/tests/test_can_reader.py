#!/usr/bin/env python3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
import can

from can_reader import CanReader, CanMapError, load_can_map
from j1939 import PGN_EEC1


def write_map(tmpdir: Path, signals: dict) -> Path:
    path = Path(tmpdir) / "can_map.yaml"
    path.write_text(yaml.safe_dump({"signals": signals}))
    return path


RPM_SIGNAL = {
    "protocol": "j1939",
    "pgn": PGN_EEC1,
    "spn": 190,
    "byte_offset": 3,
    "length_bytes": 2,
    "byte_order": "little",
    "resolution": 0.125,
    "offset": 0.0,
    "source_address": 0,
    "confirmed": True,
}

RAW_SIGNAL = {
    "protocol": "raw",
    "byte_offset": 0,
    "length_bytes": 2,
    "byte_order": "big",
    "resolution": 1.0,
    "offset": 0.0,
    "can_id": 0x123,
    "is_extended": False,
    "confirmed": True,
}


class TestLoadCanMap(unittest.TestCase):
    def test_missing_file_raises_canmaperror(self):
        with self.assertRaises(CanMapError):
            load_can_map(Path("/nonexistent/can_map.yaml"))

    def test_unconfirmed_signal_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = dict(RPM_SIGNAL)
            entry["confirmed"] = False
            path = write_map(tmp, {"rpm": entry})
            specs = load_can_map(path)
            self.assertNotIn("rpm", specs)

    def test_confirmed_signal_is_loaded_with_correct_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_map(tmp, {"rpm": RPM_SIGNAL})
            specs = load_can_map(path)
            self.assertIn("rpm", specs)
            spec = specs["rpm"]
            self.assertEqual(spec.pgn, PGN_EEC1)
            self.assertEqual(spec.byte_offset, 3)
            self.assertEqual(spec.resolution, 0.125)
            self.assertEqual(spec.source_address, 0)


class TestCanReaderMessageMatching(unittest.TestCase):
    def _make_reader(self, signals: dict) -> CanReader:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_map(tmp, signals)
            # CanReader.__init__ only loads the map; it does not open a bus.
            return CanReader(channel="vcan0", bustype="socketcan", bitrate=250000, can_map_path=path)

    def test_j1939_message_matches_by_pgn_and_source_address(self):
        reader = self._make_reader({"rpm": RPM_SIGNAL})
        spec = reader.signals["rpm"]

        can_id = (3 << 26) | (0xF0 << 16) | (0x04 << 8) | 0x00  # EEC1, SA=0
        msg = can.Message(arbitration_id=can_id, is_extended_id=True, data=bytes(8))
        self.assertTrue(reader._message_matches(msg, spec))

    def test_j1939_message_rejected_on_wrong_source_address(self):
        reader = self._make_reader({"rpm": RPM_SIGNAL})
        spec = reader.signals["rpm"]

        can_id = (3 << 26) | (0xF0 << 16) | (0x04 << 8) | 0x07  # different SA
        msg = can.Message(arbitration_id=can_id, is_extended_id=True, data=bytes(8))
        self.assertFalse(reader._message_matches(msg, spec))

    def test_j1939_message_rejected_if_not_extended(self):
        reader = self._make_reader({"rpm": RPM_SIGNAL})
        spec = reader.signals["rpm"]
        msg = can.Message(arbitration_id=0x123, is_extended_id=False, data=bytes(8))
        self.assertFalse(reader._message_matches(msg, spec))

    def test_raw_message_matches_by_id(self):
        reader = self._make_reader({"raw_sig": RAW_SIGNAL})
        spec = reader.signals["raw_sig"]
        msg = can.Message(arbitration_id=0x123, is_extended_id=False, data=bytes(8))
        self.assertTrue(reader._message_matches(msg, spec))

    def test_raw_message_rejected_on_different_id(self):
        reader = self._make_reader({"raw_sig": RAW_SIGNAL})
        spec = reader.signals["raw_sig"]
        msg = can.Message(arbitration_id=0x456, is_extended_id=False, data=bytes(8))
        self.assertFalse(reader._message_matches(msg, spec))


class TestMonotonicConversion(unittest.TestCase):
    def test_to_monotonic_offset_sampled_once(self):
        reader = CanReader.__new__(CanReader)  # bypass __init__ (no map needed for this unit)
        reader._wall_to_mono_offset = None

        t_wall_1 = time.time()
        t_mono_1 = reader._to_monotonic(t_wall_1)
        offset_after_first = reader._wall_to_mono_offset
        self.assertIsNotNone(offset_after_first)

        # A later wall-clock timestamp should map onto the same monotonic
        # axis using the SAME offset, not re-sample it.
        t_wall_2 = t_wall_1 + 5.0
        t_mono_2 = reader._to_monotonic(t_wall_2)
        self.assertEqual(reader._wall_to_mono_offset, offset_after_first)
        self.assertAlmostEqual(t_mono_2 - t_mono_1, 5.0, places=2)


if __name__ == "__main__":
    unittest.main()
