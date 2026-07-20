#!/usr/bin/env python3
"""
clock.py - shared monotonic clock + generic fixed-rate sampler, so more
than one sensor can be acquired at once without each inventing its own
timing loop or timebase.

SharedClock (shared time source) -> Ticker (fixed-rate deadlines on it)
-> RealTimeSampler (read/assemble/health loop, sensor-agnostic) ->
SensorHub (registers several RealTimeSamplers on one SharedClock).

See CLOCKING.md for the design rationale and a worked multi-sensor example.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


class SharedClock:
    """The single monotonic time source sensors timestamp against -- one
    shared "t=0" so independently-started samplers land on the same axis,
    and tests can inject a fake time source for deterministic timing."""

    def __init__(self, time_source: Callable[[], int] = time.monotonic_ns):
        self._time_source = time_source
        self._origin_ns = time_source()

    def now_ns(self) -> int:
        return self._time_source()

    @property
    def origin_ns(self) -> int:
        return self._origin_ns

    def elapsed_ns(self) -> int:
        return self.now_ns() - self._origin_ns


class Ticker:
    """Fixed-rate deadline scheduler anchored to a SharedClock's origin
    (not "now" at construction), so Tickers at different rates on the same
    clock land on a shared grid instead of drifting apart."""

    def __init__(self, clock: SharedClock, period_ns: int):
        self._clock = clock
        self.period_ns = period_ns
        self._next_deadline_ns = self._next_grid_deadline()

    def _next_grid_deadline(self) -> int:
        elapsed = self._clock.now_ns() - self._clock.origin_ns
        ticks_elapsed = elapsed // self.period_ns
        return self._clock.origin_ns + (ticks_elapsed + 1) * self.period_ns

    def wait_for_next_tick(self, spin_margin_ns: int = 100_000) -> tuple:
        """Sleeps + spins until the next deadline. Returns (deadline_ns,
        missed); on a miss, re-syncs to the next grid point from "now"
        instead of queuing up the backlog."""
        remaining_ns = self._next_deadline_ns - self._clock.now_ns()
        missed = remaining_ns <= 0

        if not missed:
            if remaining_ns > spin_margin_ns:
                time.sleep((remaining_ns - spin_margin_ns) / 1e9)
            while self._clock.now_ns() < self._next_deadline_ns:
                pass

        deadline_ns = self._next_deadline_ns
        if missed:
            self._next_deadline_ns = self._next_grid_deadline()
        else:
            self._next_deadline_ns += self.period_ns
        return deadline_ns, missed


class HealthMonitor:
    """Running interval-jitter / missed-sample / validity-error stats,
    computed online (Welford's algorithm) so it's cheap enough to update
    every tick without itself threatening the deadline. Sensor-agnostic:
    "validity" might mean CRC+RS for the SCA3300, or a checksum for another
    sensor, or nothing at all -- RealTimeSampler just forwards a bool."""

    def __init__(self, target_period_ns: int, tolerance_frac: float = 0.05, recent_cap: int = 20000):
        self._target_ns = target_period_ns
        self._tolerance_ns = int(target_period_ns * tolerance_frac)
        self._n = 0
        self._mean = 0.0
        self._m2 = 0.0
        self._min_ns = None
        self._max_ns = None
        self._missed = 0
        self._invalid = 0
        self._samples_total = 0
        # deque(maxlen) evicts in O(1); list.pop(0) here was O(n)/sample at
        # capacity -- on a 2kHz path that alone could blow the 500us budget.
        self._recent_intervals_ns: deque = deque(maxlen=recent_cap)
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

    def record_sample(self, valid: bool):
        with self._lock:
            self._samples_total += 1
            if not valid:
                self._invalid += 1

    def status(self) -> dict:
        with self._lock:
            std_ns = (self._m2 / self._n) ** 0.5 if self._n > 1 else 0.0
            if self._recent_intervals_ns:
                p99_ns = float(np.percentile(np.fromiter(self._recent_intervals_ns, dtype=np.int64), 99))
            else:
                p99_ns = None
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
                "invalid_samples": self._invalid,
                "invalid_rate": self._invalid / self._samples_total if self._samples_total else 0.0,
            }


@dataclass
class Block:
    sensor_name: str
    t0_ns: int
    samples: np.ndarray  # shape (n, n_channels)
    sample_rate_hz: float
    missed_in_block: int


def set_realtime(priority: int, cpu_core: Optional[int]) -> tuple:
    """Best-effort SCHED_FIFO + CPU pin for the calling thread (root or
    CAP_SYS_NICE required). Returns (sched_fifo_active, cpu_pinned) so
    callers can tell it fell back instead of only logging it."""
    sched_fifo_active = False
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(priority))
        sched_fifo_active = True
        logger.info("SCHED_FIFO priority %d set", priority)
    except PermissionError:
        logger.warning("could not set SCHED_FIFO (need root/CAP_SYS_NICE) -- running at normal scheduling")
    except Exception as exc:
        logger.warning("could not set SCHED_FIFO: %s", exc)

    cpu_pinned = False
    if cpu_core is not None:
        try:
            os.sched_setaffinity(0, {cpu_core})
            cpu_pinned = True
            logger.info("pinned to CPU core %d", cpu_core)
        except Exception as exc:
            logger.warning("could not pin to CPU core %d: %s", cpu_core, exc)

    return sched_fifo_active, cpu_pinned


class RealTimeSampler:
    """Generic fixed-rate sampler: reads `read_fn()` -> (values, valid),
    assembles blocks, tracks health. Knows nothing about SPI/CAN/any
    specific sensor -- that's entirely read_fn's job -- which is what lets
    several sensors share one clocking mechanism as independent threads."""

    def __init__(self, name: str, read_fn: Callable[[], tuple], n_channels: int, rate_hz: float,
                 block_size: int, clock: SharedClock, queue_maxsize: int = 8,
                 use_sched_fifo: bool = False, priority: int = 80, cpu_core: Optional[int] = None,
                 realtime_required: bool = False, spin_margin_ns: int = 50_000,
                 on_error: Optional[Callable[[Exception], None]] = None):
        self.name = name
        self._read_fn = read_fn
        self._n_channels = n_channels
        self.rate_hz = rate_hz
        self._period_ns = int(1e9 / rate_hz)
        self._block_size = block_size
        self._clock = clock
        self._queue: "queue.Queue[Block]" = queue.Queue(maxsize=queue_maxsize)
        self.health = HealthMonitor(self._period_ns)
        self._use_sched_fifo = use_sched_fifo
        self._priority = priority
        self._cpu_core = cpu_core
        self._realtime_required = realtime_required
        self._spin_margin_ns = spin_margin_ns
        self._on_error = on_error
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Set from inside the sampler thread (sched policy/affinity are
        # per-thread) and surfaced in health_status(), not just logged.
        self.sched_fifo_active = False
        self.cpu_pinned = False
        self._startup_error: Optional[Exception] = None
        self._realtime_ready = threading.Event()

    def start(self):
        self._stop_event.clear()
        self._realtime_ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run_loop, name=f"sampler-{self.name}", daemon=True)
        self._thread.start()

        if self._use_sched_fifo:
            self._realtime_ready.wait(timeout=2.0)
            if self._startup_error is not None:
                self.stop()
                raise self._startup_error

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_block(self, timeout: Optional[float] = None) -> Optional[Block]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def health_status(self) -> dict:
        status = self.health.status()
        status["sched_fifo_active"] = self.sched_fifo_active
        status["cpu_pinned"] = self.cpu_pinned
        return status

    def _run_loop(self):
        if self._use_sched_fifo:
            try:
                self.sched_fifo_active, self.cpu_pinned = set_realtime(self._priority, self._cpu_core)
                if self._realtime_required and not self.sched_fifo_active:
                    raise RuntimeError(
                        f"[{self.name}] realtime.required is set but SCHED_FIFO could not be obtained "
                        f"(need root or CAP_SYS_NICE) -- refusing to run at normal scheduling instead "
                        f"of silently degrading timing guarantees"
                    )
            except Exception as exc:
                self._startup_error = exc
                self._realtime_ready.set()
                return
        self._realtime_ready.set()

        ticker = Ticker(self._clock, self._period_ns)
        buffer = np.zeros((self._block_size, self._n_channels), dtype=np.float64)
        buf_idx = 0
        block_t0_ns = None
        missed_in_block = 0
        last_ns = self._clock.now_ns()

        while not self._stop_event.is_set():
            if block_t0_ns is None:
                block_t0_ns = self._clock.now_ns()

            valid = False
            try:
                values, valid = self._read_fn()
                if valid:
                    buffer[buf_idx] = values
                else:
                    buffer[buf_idx] = np.nan
                    missed_in_block += 1
            except Exception as exc:  # noqa: BLE001 - sensor-specific errors surface via on_error
                buffer[buf_idx] = np.nan
                missed_in_block += 1
                logger.warning("[%s] read_fn error: %s", self.name, exc)
                if self._on_error:
                    self._on_error(exc)

            self.health.record_sample(valid)
            buf_idx += 1

            now_ns = self._clock.now_ns()
            self.health.record_interval(now_ns - last_ns)
            last_ns = now_ns

            if buf_idx == self._block_size:
                block = Block(sensor_name=self.name, t0_ns=block_t0_ns, samples=buffer.copy(),
                               sample_rate_hz=self.rate_hz, missed_in_block=missed_in_block)
                self._emit_block(block)
                buf_idx = 0
                block_t0_ns = None
                missed_in_block = 0

            ticker.wait_for_next_tick(spin_margin_ns=self._spin_margin_ns)

    def _emit_block(self, block: Block):
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            try:
                self._queue.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            self._queue.put_nowait(block)
            logger.warning("[%s] block queue full -- dropped oldest block", self.name)


class SensorHub:
    """Registration point for running multiple sensors at once. Each
    add_sensor() gets its own thread/queue/fault domain, but all share
    this hub's SharedClock so block timestamps stay directly comparable
    across sensors. See CLOCKING.md for a worked multi-sensor example."""

    def __init__(self, clock: Optional[SharedClock] = None):
        self.clock = clock or SharedClock()
        self._samplers: dict = {}

    def add_sensor(self, name: str, read_fn: Callable[[], tuple], n_channels: int, rate_hz: float,
                   block_size: int, **kwargs) -> RealTimeSampler:
        if name in self._samplers:
            raise ValueError(f"sensor {name!r} already registered")
        sampler = RealTimeSampler(name, read_fn, n_channels, rate_hz, block_size, self.clock, **kwargs)
        self._samplers[name] = sampler
        return sampler

    def sensors(self) -> list:
        return list(self._samplers.keys())

    def start_all(self):
        started = []
        try:
            for sampler in self._samplers.values():
                sampler.start()
                started.append(sampler)
        except Exception:
            # Don't leave earlier sensors running as orphaned threads if a
            # later one fails to start.
            for sampler in started:
                sampler.stop()
            raise

    def stop_all(self):
        for sampler in self._samplers.values():
            sampler.stop()

    def get_block(self, name: str, timeout: Optional[float] = None) -> Optional[Block]:
        return self._samplers[name].get_block(timeout)

    def health(self, name: Optional[str] = None) -> dict:
        if name is not None:
            return self._samplers[name].health_status()
        return {n: s.health_status() for n, s in self._samplers.items()}
