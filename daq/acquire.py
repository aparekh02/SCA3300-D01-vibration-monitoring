#!/usr/bin/env python3
"""
acquire.py - Task 2 entry point: deterministic vibration acquisition.

Registers the SCA3300 as one sensor on a clock.SensorHub, which drives it
at a strict 2kHz on a dedicated real-time thread (monotonic-timer driven,
not sensor-interrupt or FIFO driven -- the -D01 breakout has neither),
assembling fixed-length evenly-sampled blocks and tracking health
(jitter/missed-samples/CRC error rate). The hub is the extension point for
more sensors -- see CLOCKING.md for how a second sensor would be added and
still share the same clock/timebase while running independently.

No FFT/diagnostics here -- only acquisition + alignment plumbing, per brief.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

from sca3300 import SCA3300, SCA3300Error, RS_ERROR
from can_reader import CanReader, CanMapError
from clock import SensorHub, Block

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("acquire")

HERE = Path(__file__).resolve().parent

VIBRATION_SENSOR_NAME = "vibration"


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def make_sca3300_read_fn(sca: SCA3300):
    """Adapts SCA3300.read_accel()'s (values, rs, crc_ok) into the generic
    RealTimeSampler contract: (values, valid). On any comms error, attempts
    a clean re-init and reports the sample invalid rather than propagating
    a stale/partial reading -- matches "never emit unvalidated samples"."""

    def read_fn():
        try:
            (x, y, z), rs, crc_ok = sca.read_accel()
            valid = crc_ok and rs != RS_ERROR
            if not valid:
                logger.warning("invalid sample (crc_ok=%s rs=%d) -- reinitializing", crc_ok, rs)
                _safe_reinit(sca)
            return (x, y, z), valid
        except SCA3300Error as exc:
            logger.warning("comms error reading sample: %s -- reinitializing", exc)
            _safe_reinit(sca)
            return (0.0, 0.0, 0.0), False

    return read_fn


def _safe_reinit(sca: SCA3300):
    try:
        sca.reinit()
    except SCA3300Error as exc:
        logger.error("reinit failed: %s", exc)


class Acquirer:
    """Thin SCA3300-specific wrapper around a clock.SensorHub with exactly
    one sensor registered. Kept as a small class (rather than inlining
    everything in main()) so probe-style tools/tests can drive it directly
    without going through argparse/CLI."""

    def __init__(self, cfg: dict, hub: Optional[SensorHub] = None):
        self._cfg = cfg
        spi_cfg = cfg["spi"]
        self._sca = SCA3300(bus=spi_cfg["bus"], device=spi_cfg["device"],
                             max_speed_hz=spi_cfg["max_speed_hz"], mode=spi_cfg.get("mode", 1))
        self.hub = hub or SensorHub()

        sampling_cfg = cfg["sampling"]
        rt_cfg = cfg.get("realtime", {})
        log_cfg = cfg.get("logging", {})

        self._write_to_disk = log_cfg.get("write_blocks_to_disk", False)
        self._raw_dir = Path(log_cfg.get("raw_dir", "data/raw"))
        if self._write_to_disk:
            self._raw_dir.mkdir(parents=True, exist_ok=True)

        self._sampler = self.hub.add_sensor(
            VIBRATION_SENSOR_NAME,
            read_fn=make_sca3300_read_fn(self._sca),
            n_channels=3,
            rate_hz=sampling_cfg["rate_hz"],
            block_size=sampling_cfg["block_size"],
            queue_maxsize=sampling_cfg.get("queue_maxsize", 8),
            use_sched_fifo=rt_cfg.get("use_sched_fifo", True),
            priority=rt_cfg.get("priority", 80),
            cpu_core=rt_cfg.get("cpu_core"),
        )

    def start(self):
        self._sca.start_up()
        self.hub.start_all()

    def stop(self):
        self.hub.stop_all()
        self._sca.close()

    def get_block(self, timeout: Optional[float] = None) -> Optional[Block]:
        block = self.hub.get_block(VIBRATION_SENSOR_NAME, timeout=timeout)
        if block is not None and self._write_to_disk:
            self._write_block(block)
        return block

    def health_status(self) -> dict:
        return self.hub.health(VIBRATION_SENSOR_NAME)

    def _write_block(self, block: Block):
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
