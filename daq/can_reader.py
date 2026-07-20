#!/usr/bin/env python3
"""
can_reader.py - background CAN signal reader for Task 2's acquisition path.

Subscribes to the signals defined in can_map.yaml (produced by a human
confirming candidates from can_discover.py's can_map.todo.yaml output),
decodes engineering units, and keeps a (monotonic_timestamp, value)
time-series per signal on the SAME monotonic clock as the vibration path
(time.monotonic()), so align.py can interpolate one against the other.

python-can's SocketCAN backend timestamps frames from the kernel
(SO_TIMESTAMP, CLOCK_REALTIME-based), not Python wall-clock-at-receive.
Converting that to our monotonic timebase requires one wall<->monotonic
offset sample at startup; see README for the caveat this introduces if
CLOCK_REALTIME steps (e.g. an NTP correction) mid-run. slcan adapters
generally do NOT provide a kernel timestamp at all -- can_discover.py flags
this per-adapter so the caller knows which timestamp quality to expect.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

import can

from j1939 import decode_29bit_id, decode_signal

logger = logging.getLogger(__name__)


class CanMapError(Exception):
    pass


@dataclass
class SignalSpec:
    name: str
    protocol: str  # "j1939" or "raw"
    byte_offset: int
    length_bytes: int
    byte_order: str
    resolution: float
    offset: float
    pgn: Optional[int] = None
    can_id: Optional[int] = None
    is_extended: Optional[bool] = None
    source_address: Optional[int] = None


def load_can_map(path: Path) -> dict:
    if not path.exists():
        raise CanMapError(
            f"{path} not found. Run can_discover.py, review can_map.todo.yaml, "
            f"confirm entries against a real spin-up/throttle change, then save "
            f"the confirmed signals as {path.name}."
        )
    with open(path) as f:
        doc = yaml.safe_load(f) or {}

    specs = {}
    for name, entry in (doc.get("signals") or {}).items():
        if not entry.get("confirmed", False):
            logger.warning("skipping unconfirmed can_map signal %r (confirmed: false)", name)
            continue
        specs[name] = SignalSpec(
            name=name,
            protocol=entry["protocol"],
            byte_offset=entry["byte_offset"],
            length_bytes=entry["length_bytes"],
            byte_order=entry.get("byte_order", "little"),
            resolution=entry["resolution"],
            offset=entry.get("offset", 0.0),
            pgn=entry.get("pgn"),
            can_id=entry.get("can_id"),
            is_extended=entry.get("is_extended"),
            source_address=entry.get("source_address"),
        )
    return specs


class CanReader:
    """Background thread subscribing to mapped signals over python-can,
    storing (monotonic_seconds, value) series for align.py to consume."""

    def __init__(self, channel: str, bustype: str, bitrate: int, can_map_path: Path,
                 gap_timeout_s: float = 1.0, series_maxlen: int = 20000):
        self.signals = load_can_map(can_map_path)
        self._channel = channel
        self._bustype = bustype
        self._bitrate = bitrate
        self._gap_timeout_s = gap_timeout_s
        self._series = {name: deque(maxlen=series_maxlen) for name in self.signals}
        self._lock = threading.Lock()
        self._bus: Optional[can.BusABC] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wall_to_mono_offset: Optional[float] = None
        self.gap_count = 0
        self.last_message_mono: Optional[float] = None

    def start(self):
        self._bus = can.interface.Bus(channel=self._channel, interface=self._bustype, bitrate=self._bitrate)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="can_reader", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._bus:
            self._bus.shutdown()

    def _to_monotonic(self, msg_timestamp: float) -> float:
        """Map python-can's (wall-clock-epoch) msg.timestamp onto our
        time.monotonic() timebase using a one-time offset sampled at the
        first received frame. See module docstring for the drift caveat."""
        if self._wall_to_mono_offset is None:
            self._wall_to_mono_offset = time.monotonic() - time.time()
        return msg_timestamp + self._wall_to_mono_offset

    def _run(self):
        while not self._stop_event.is_set():
            msg = self._bus.recv(timeout=self._gap_timeout_s)
            now_mono = time.monotonic()
            if msg is None:
                if self.last_message_mono is not None:
                    gap = now_mono - self.last_message_mono
                    self.gap_count += 1
                    logger.warning("CAN bus gap: no message for %.2fs", gap)
                continue

            self.last_message_mono = now_mono
            t_mono = self._to_monotonic(msg.timestamp)

            for name, spec in self.signals.items():
                if not self._message_matches(msg, spec):
                    continue
                try:
                    value = decode_signal(bytes(msg.data), spec.byte_offset, spec.length_bytes,
                                           spec.resolution, spec.offset, spec.byte_order)
                except ValueError as exc:
                    logger.warning("failed to decode signal %r from id=0x%X: %s", name, msg.arbitration_id, exc)
                    continue
                with self._lock:
                    self._series[name].append((t_mono, value))

    def _message_matches(self, msg, spec: SignalSpec) -> bool:
        if spec.protocol == "j1939":
            if not msg.is_extended_id:
                return False
            j = decode_29bit_id(msg.arbitration_id)
            if j.pgn != spec.pgn:
                return False
            if spec.source_address is not None and j.source_address != spec.source_address:
                return False
            return True
        elif spec.protocol == "raw":
            if spec.is_extended is not None and msg.is_extended_id != spec.is_extended:
                return False
            return msg.arbitration_id == spec.can_id
        return False

    def series_snapshot(self, name: str) -> list:
        """Thread-safe copy of the (t_mono, value) series for a signal."""
        with self._lock:
            return list(self._series.get(name, ()))
