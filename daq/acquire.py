#!/usr/bin/env python3
"""
acquire.py - Task 2 entry point: deterministic vibration acquisition.

Runs a dedicated real-time thread reading the SCA3300 at a strict 2kHz
(monotonic-timer driven, not sensor-interrupt or FIFO driven -- the -D01
breakout has neither), assembles fixed-length evenly-sampled blocks, tracks
health (jitter/missed-samples/CRC error rate), and optionally starts a
CanReader so blocks can later be aligned to RPM via align.py.

No FFT/diagnostics here -- only acquisition + alignment plumbing, per brief.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

from sca3300 import SCA3300, SCA3300Error, RS_ERROR
from can_reader import CanReader, CanMapError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("acquire")

HERE = Path(__file__).resolve().parent


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


@dataclass
class Block:
    t0_ns: int
    samples: np.ndarray  # shape (n, 3), columns x/y/z in g
    sample_rate_hz: float
    missed_in_block: int


class HealthMonitor:
    """Running interval-jitter / missed-sample / CRC-error stats, computed
    online (Welford's algorithm) so it's cheap enough to update every
    500us tick without itself threatening the deadline."""

    def __init__(self, target_period_ns: int, tolerance_frac: float = 0.05):
        self._target_ns = target_period_ns
        self._tolerance_ns = int(target_period_ns * tolerance_frac)
        self._n = 0
        self._mean = 0.0
        self._m2 = 0.0
        self._min_ns = None
        self._max_ns = None
        self._missed = 0
        self._crc_errors = 0
        self._samples_total = 0
        self._recent_intervals_ns = []  # bounded window for p99
        self._recent_cap = 20000
        self._lock = threading.Lock()

    def record_interval(self, interval_ns: int):
        with self._lock:
            self._n += 1
            delta = interval_ns - self._mean
            self._mean += delta / self._n
            self._m2 += delta * (interval_ns - self._mean)
            self._min_ns = interval_ns if self._min_ns is None else min(self._min_ns, interval_ns)
            self._max_ns = interval_ns if self._max_ns is None else max(self._max_ns, interval_ns)
            if abs(interval_ns - self._target_ns) > self._tolerance_ns:
                self._missed += 1
            self._recent_intervals_ns.append(interval_ns)
            if len(self._recent_intervals_ns) > self._recent_cap:
                self._recent_intervals_ns.pop(0)

    def record_sample(self, crc_ok: bool):
        with self._lock:
            self._samples_total += 1
            if not crc_ok:
                self._crc_errors += 1

    def status(self) -> dict:
        with self._lock:
            std_ns = (self._m2 / self._n) ** 0.5 if self._n > 1 else 0.0
            p99_ns = float(np.percentile(self._recent_intervals_ns, 99)) if self._recent_intervals_ns else None
            return {
                "intervals_recorded": self._n,
                "mean_us": self._mean / 1000.0,
                "std_us": std_ns / 1000.0,
                "min_us": (self._min_ns or 0) / 1000.0,
                "max_us": (self._max_ns or 0) / 1000.0,
                "p99_us": (p99_ns / 1000.0) if p99_ns is not None else None,
                "missed_count": self._missed,
                "missed_pct": 100.0 * self._missed / self._n if self._n else 0.0,
                "samples_total": self._samples_total,
                "crc_errors": self._crc_errors,
                "crc_error_rate": self._crc_errors / self._samples_total if self._samples_total else 0.0,
            }


def _try_set_realtime(priority: int, cpu_core: Optional[int]):
    """Best-effort SCHED_FIFO + CPU pin for the calling thread. Requires
    root or CAP_SYS_NICE; logs and continues at normal scheduling if denied
    (Task 2 still runs -- just without the real-time guarantee, which is
    exactly what probe_sca3300.py's timing numbers are meant to catch)."""
    try:
        param = os.sched_param(priority)
        os.sched_setscheduler(0, os.SCHED_FIFO, param)
        logger.info("SCHED_FIFO priority %d set", priority)
    except PermissionError:
        logger.warning("could not set SCHED_FIFO (need root/CAP_SYS_NICE) -- running at normal scheduling")
    except Exception as exc:
        logger.warning("could not set SCHED_FIFO: %s", exc)

    if cpu_core is not None:
        try:
            os.sched_setaffinity(0, {cpu_core})
            logger.info("pinned to CPU core %d", cpu_core)
        except Exception as exc:
            logger.warning("could not pin to CPU core %d: %s", cpu_core, exc)


class Acquirer:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        spi_cfg = cfg["spi"]
        self._sca = SCA3300(bus=spi_cfg["bus"], device=spi_cfg["device"],
                             max_speed_hz=spi_cfg["max_speed_hz"], mode=spi_cfg.get("mode", 1))
        self._rate_hz = cfg["sampling"]["rate_hz"]
        self._period_ns = int(1e9 / self._rate_hz)
        self._block_size = cfg["sampling"]["block_size"]
        self._queue: "queue.Queue[Block]" = queue.Queue(maxsize=cfg["sampling"].get("queue_maxsize", 8))
        self._health = HealthMonitor(self._period_ns)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        rt_cfg = cfg.get("realtime", {})
        self._use_sched_fifo = rt_cfg.get("use_sched_fifo", True)
        self._priority = rt_cfg.get("priority", 80)
        self._cpu_core = rt_cfg.get("cpu_core")

        log_cfg = cfg.get("logging", {})
        self._write_to_disk = log_cfg.get("write_blocks_to_disk", False)
        self._raw_dir = Path(log_cfg.get("raw_dir", "data/raw"))
        if self._write_to_disk:
            self._raw_dir.mkdir(parents=True, exist_ok=True)

    def start(self):
        self._sca.start_up()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="sca3300_sampler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._sca.close()

    def get_block(self, timeout: Optional[float] = None) -> Optional[Block]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def health_status(self) -> dict:
        return self._health.status()

    def _run_loop(self):
        if self._use_sched_fifo:
            _try_set_realtime(self._priority, self._cpu_core)

        buffer = np.zeros((self._block_size, 3), dtype=np.float64)
        buf_idx = 0
        block_t0_ns = None
        missed_in_block = 0

        last_ns = time.monotonic_ns()
        next_tick_ns = last_ns + self._period_ns

        while not self._stop_event.is_set():
            if block_t0_ns is None:
                block_t0_ns = time.monotonic_ns()

            crc_ok = False
            try:
                (x, y, z), rs, crc_ok = self._sca.read_accel()
                if crc_ok and rs != RS_ERROR:
                    buffer[buf_idx] = (x, y, z)
                else:
                    buffer[buf_idx] = (np.nan, np.nan, np.nan)
                    missed_in_block += 1
                    logger.warning("invalid sample (crc_ok=%s rs=%d) -- reinitializing", crc_ok, rs)
                    self._safe_reinit()
            except SCA3300Error as exc:
                buffer[buf_idx] = (np.nan, np.nan, np.nan)
                missed_in_block += 1
                logger.warning("comms error reading sample: %s -- reinitializing", exc)
                self._safe_reinit()

            self._health.record_sample(crc_ok)
            buf_idx += 1

            now_ns = time.monotonic_ns()
            self._health.record_interval(now_ns - last_ns)
            last_ns = now_ns

            if buf_idx == self._block_size:
                block = Block(t0_ns=block_t0_ns, samples=buffer.copy(),
                               sample_rate_hz=self._rate_hz, missed_in_block=missed_in_block)
                self._emit_block(block)
                buf_idx = 0
                block_t0_ns = None
                missed_in_block = 0

            next_tick_ns += self._period_ns
            remaining_ns = next_tick_ns - time.monotonic_ns()
            if remaining_ns > 0:
                # Sleep most of the remainder, then busy-wait the last
                # ~100us for tighter precision than time.sleep() alone
                # reliably gives on a non-isolated core.
                if remaining_ns > 150_000:
                    time.sleep((remaining_ns - 100_000) / 1e9)
                while time.monotonic_ns() < next_tick_ns:
                    pass
            else:
                next_tick_ns = time.monotonic_ns()

    def _safe_reinit(self):
        try:
            self._sca.reinit()
        except SCA3300Error as exc:
            logger.error("reinit failed: %s", exc)

    def _emit_block(self, block: Block):
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            try:
                self._queue.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            self._queue.put_nowait(block)
            logger.warning("block queue full -- dropped oldest block")

        if self._write_to_disk:
            fname = self._raw_dir / f"block_{block.t0_ns}.npz"
            np.savez(fname, samples=block.samples, t0_ns=block.t0_ns,
                     sample_rate_hz=block.sample_rate_hz, missed_in_block=block.missed_in_block)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(HERE / "config.yaml"))
    parser.add_argument("--duration", type=float, default=0.0, help="0 = run until Ctrl+C")
    parser.add_argument("--no-can", action="store_true", help="skip starting the CAN reader")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    acquirer = Acquirer(cfg)

    can_reader = None
    can_cfg = cfg.get("can")
    if can_cfg and not args.no_can:
        map_path = HERE / can_cfg.get("map_file", "can_map.yaml")
        try:
            can_reader = CanReader(
                channel=can_cfg["channel"], bustype=can_cfg["bustype"], bitrate=can_cfg["bitrate"],
                can_map_path=map_path, gap_timeout_s=can_cfg.get("gap_timeout_s", 1.0),
            )
            can_reader.start()
            logger.info("CAN reader started on %s", can_cfg["channel"])
        except CanMapError as exc:
            logger.warning("CAN reader not started: %s", exc)
        except Exception as exc:
            logger.warning("CAN reader failed to start: %s", exc)

    stop_requested = threading.Event()

    def _handle_sigint(signum, frame):
        stop_requested.set()

    signal.signal(signal.SIGINT, _handle_sigint)

    logger.info("Starting SCA3300 acquisition (rate=%dHz, block_size=%d)",
                cfg["sampling"]["rate_hz"], cfg["sampling"]["block_size"])
    acquirer.start()

    health_interval = cfg.get("logging", {}).get("health_log_interval_s", 5)
    start_time = time.monotonic()
    last_health_log = start_time
    blocks_emitted = 0

    try:
        while not stop_requested.is_set():
            if args.duration and (time.monotonic() - start_time) >= args.duration:
                break
            block = acquirer.get_block(timeout=0.5)
            if block is not None:
                blocks_emitted += 1
                logger.info("block %d ready: t0_ns=%d missed=%d",
                            blocks_emitted, block.t0_ns, block.missed_in_block)

            if time.monotonic() - last_health_log >= health_interval:
                logger.info("health: %s", acquirer.health_status())
                last_health_log = time.monotonic()
    finally:
        logger.info("stopping...")
        acquirer.stop()
        if can_reader:
            can_reader.stop()
        logger.info("final health: %s", acquirer.health_status())


if __name__ == "__main__":
    main()
