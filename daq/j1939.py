#!/usr/bin/env python3
"""
j1939.py - shared SAE J1939 arbitration-ID and signal decode helpers.

The 29-bit ID -> PGN/source-address split is the standard, publicly
documented J1939 bit layout (not vendor-specific). The specific SPN byte
offsets below (EEC1 engine speed/torque, EEC2 load) follow the commonly
published SAE J1939-71 layout used by most open engine ECU DBC files; they
are candidates for can_discover.py to flag, not guaranteed for any given
vessel's ECU -- can_map.todo.yaml exists precisely so a human confirms them
against a real spin-up/throttle change before they're trusted.
"""

from __future__ import annotations

from dataclasses import dataclass

PGN_EEC1 = 61444  # Electronic Engine Controller 1
PGN_EEC2 = 61443  # Electronic Engine Controller 2

# (pgn, spn) -> (name, byte_offset [0-indexed into the 8-byte payload],
#                length_bytes, byte_order, resolution, offset, unit)
KNOWN_SIGNALS = {
    (PGN_EEC1, 190): ("engine_speed", 3, 2, "little", 0.125, 0.0, "rpm"),
    (PGN_EEC1, 513): ("engine_actual_torque_pct", 2, 1, "little", 1.0, -125.0, "%"),
    (PGN_EEC2, 92): ("engine_percent_load_at_current_speed", 2, 1, "little", 1.0, 0.0, "%"),
    (PGN_EEC2, 91): ("accelerator_pedal_position", 1, 1, "little", 0.4, 0.0, "%"),
}


@dataclass(frozen=True)
class J1939Id:
    priority: int
    pgn: int
    source_address: int
    pdu_format: int
    pdu_specific: int
    is_peer_to_peer: bool  # PDU1 (PF < 240): pdu_specific is a destination address, not part of the PGN


def decode_29bit_id(can_id: int) -> J1939Id:
    """Standard SAE J1939 decomposition of a 29-bit extended arbitration ID."""
    can_id &= 0x1FFFFFFF
    priority = (can_id >> 26) & 0x7
    reserved_dp = (can_id >> 24) & 0x3  # reserved bit + data page bit
    pdu_format = (can_id >> 16) & 0xFF
    pdu_specific = (can_id >> 8) & 0xFF
    source_address = can_id & 0xFF

    is_peer_to_peer = pdu_format < 240
    if is_peer_to_peer:
        pgn = (reserved_dp << 16) | (pdu_format << 8)
    else:
        pgn = (reserved_dp << 16) | (pdu_format << 8) | pdu_specific

    return J1939Id(
        priority=priority,
        pgn=pgn,
        source_address=source_address,
        pdu_format=pdu_format,
        pdu_specific=pdu_specific,
        is_peer_to_peer=is_peer_to_peer,
    )


def extract_raw(data: bytes, byte_offset: int, length_bytes: int, byte_order: str = "little") -> int:
    chunk = data[byte_offset:byte_offset + length_bytes]
    if len(chunk) < length_bytes:
        raise ValueError(f"payload too short: need {length_bytes} bytes at offset {byte_offset}, got {len(chunk)}")
    return int.from_bytes(chunk, byteorder=byte_order, signed=False)


def decode_signal(data: bytes, byte_offset: int, length_bytes: int, resolution: float,
                   offset: float, byte_order: str = "little") -> float:
    raw = extract_raw(data, byte_offset, length_bytes, byte_order)
    return raw * resolution + offset


def candidates_for_pgn(pgn: int) -> list:
    """Returns [(spn, name, unit), ...] of known candidate signals in a PGN."""
    return [(spn, name, unit) for (p, spn), (name, *_rest, unit) in KNOWN_SIGNALS.items() if p == pgn]
