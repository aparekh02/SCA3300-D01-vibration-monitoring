#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from j1939 import (
    decode_29bit_id, decode_signal, extract_raw, candidates_for_pgn,
    PGN_EEC1, PGN_EEC2,
)


def make_j1939_id(priority: int, pgn: int, source_address: int) -> int:
    pdu_format = (pgn >> 8) & 0xFF
    if pdu_format < 240:
        # peer-to-peer (PDU1): PS byte is a destination address, not part of PGN
        pdu_specific = 0x00
    else:
        pdu_specific = pgn & 0xFF
    reserved_dp = (pgn >> 16) & 0x3
    return (priority << 26) | (reserved_dp << 24) | (pdu_format << 16) | (pdu_specific << 8) | source_address


class TestDecode29BitId(unittest.TestCase):
    def test_broadcast_pgn_eec1(self):
        can_id = make_j1939_id(priority=3, pgn=PGN_EEC1, source_address=0x00)
        j = decode_29bit_id(can_id)
        self.assertEqual(j.pgn, PGN_EEC1)
        self.assertEqual(j.priority, 3)
        self.assertEqual(j.source_address, 0x00)
        self.assertFalse(j.is_peer_to_peer)

    def test_broadcast_pgn_eec2_different_source(self):
        can_id = make_j1939_id(priority=6, pgn=PGN_EEC2, source_address=0x17)
        j = decode_29bit_id(can_id)
        self.assertEqual(j.pgn, PGN_EEC2)
        self.assertEqual(j.source_address, 0x17)

    def test_peer_to_peer_pdu1_excludes_ps_from_pgn(self):
        # PF=0xEF (< 240) is a PDU1/peer-to-peer format; PGN must not include PS.
        can_id = (3 << 26) | (0 << 24) | (0xEF << 16) | (0x22 << 8) | 0x05
        j = decode_29bit_id(can_id)
        self.assertTrue(j.is_peer_to_peer)
        self.assertEqual(j.pgn, 0xEF00)
        self.assertEqual(j.pdu_specific, 0x22)

    def test_masks_id_to_29_bits(self):
        can_id_with_junk_high_bits = make_j1939_id(3, PGN_EEC1, 0x00) | (0xF << 29)
        j = decode_29bit_id(can_id_with_junk_high_bits)
        self.assertEqual(j.pgn, PGN_EEC1)


class TestSignalDecode(unittest.TestCase):
    def test_engine_speed_spn190(self):
        raw = int(1500.0 / 0.125)
        payload = bytearray(8)
        payload[3] = raw & 0xFF
        payload[4] = (raw >> 8) & 0xFF
        value = decode_signal(bytes(payload), byte_offset=3, length_bytes=2, resolution=0.125, offset=0.0)
        self.assertAlmostEqual(value, 1500.0)

    def test_torque_spn513_with_negative_offset(self):
        payload = bytearray(8)
        payload[2] = 40 + 125  # 40% actual torque, offset -125
        value = decode_signal(bytes(payload), byte_offset=2, length_bytes=1, resolution=1.0, offset=-125.0)
        self.assertAlmostEqual(value, 40.0)

    def test_extract_raw_too_short_raises(self):
        with self.assertRaises(ValueError):
            extract_raw(bytes([0x00, 0x01]), byte_offset=3, length_bytes=2)

    def test_extract_raw_big_endian(self):
        value = extract_raw(bytes([0x01, 0x02]), byte_offset=0, length_bytes=2, byte_order="big")
        self.assertEqual(value, 0x0102)


class TestCandidatesForPgn(unittest.TestCase):
    def test_eec1_has_speed_and_torque(self):
        names = {name for _, name, _ in candidates_for_pgn(PGN_EEC1)}
        self.assertIn("engine_speed", names)
        self.assertIn("engine_actual_torque_pct", names)

    def test_unknown_pgn_returns_empty(self):
        self.assertEqual(candidates_for_pgn(999999), [])


if __name__ == "__main__":
    unittest.main()
