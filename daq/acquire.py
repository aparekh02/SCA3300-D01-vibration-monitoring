#!/usr/bin/env python3
"""
acquire.py - Task 2 entry point: deterministic vibration acquisition.

Registers every sensor in config.yaml's `sensors:` list on a
clock.SensorHub, each on its own real-time thread (monotonic-timer
driven, not interrupt/FIFO -- the -D01 breakout has neither), assembling
evenly-sampled blocks and tracking health per sensor. Adding a sensor is a
config edit, not a code change -- see CLOCKING.md; only `type: sca3300` is
implemented since that's the only sensor this build has hardware for.

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
    """Registers every `sensors:` entry from config.yaml onto a
    clock.SensorHub. With exactly one sensor, get_block()/health_status()
    work without naming it; with more than one, callers must say which."""

    def __init__(self, cfg: dict, hub: Optional[SensorHub] = None):
        self._cfg = cfg
        self.hub = hub or SensorHub()
        self._scas: dict = {}

        log_cfg = cfg.get("logging", {})
        self._write_to_disk = log_cfg.get("write_blocks_to_disk", False)
        self._raw_dir = Path(log_cfg.get("raw_dir", "data/raw"))
        if self._write_to_disk:
            self._raw_dir.mkdir(parents=True, exist_ok=True)

        sensors_cfg = cfg["sensors"]
        if not sensors_cfg:
            raise ValueError("config.yaml's 'sensors:' list is empty -- nothing to acquire")
        for sensor_cfg in sensors_cfg:
            self._register_sensor(sensor_cfg)

    def _register_sensor(self, sensor_cfg: dict):
        name = sensor_cfg["name"]
        sensor_type = sensor_cfg.get("type", "sca3300")
        if sensor_type != "sca3300":
            raise ValueError(
                f"sensor {name!r}: unsupported type {sensor_type!r} -- only 'sca3300' is "
                f"implemented; teach this method how to build its read_fn to add another type"
            )

        spi_cfg = sensor_cfg["spi"]
        sca = SCA3300(bus=spi_cfg["bus"], device=spi_cfg["device"],
                      max_speed_hz=spi_cfg["max_speed_hz"], mode=spi_cfg.get("mode", 1))
        self._scas[name] = sca

        sampling_cfg = sensor_cfg["sampling"]
        rt_cfg = sensor_cfg.get("realtime", {})
        self.hub.add_sensor(
            name,
            read_fn=make_sca3300_read_fn(sca),
            n_channels=3,
            rate_hz=sampling_cfg["rate_hz"],
            block_size=sampling_cfg["block_size"],
            queue_maxsize=sampling_cfg.get("queue_maxsize", 8),
            use_sched_fifo=rt_cfg.get("use_sched_fifo", True),
            realtime_required=rt_cfg.get("required", False),
            priority=rt_cfg.get("priority", 80),
            cpu_core=rt_cfg.get("cpu_core"),
            spin_margin_ns=int(rt_cfg.get("spin_margin_us", 100) * 1000),
        )

    def sensor_names(self) -> list:
        return list(self._scas.keys())

    def start(self):
        started = []
        try:
            for sca in self._scas.values():
                sca.start_up()
                started.append(sca)
            self.hub.start_all()
        except Exception:
            # Don't leave earlier sensors' SPI links initialized (and their
            # samplers possibly already running) if a later step fails.
            self.hub.stop_all()
            for sca in started:
                sca.close()
            raise

    def stop(self):
        self.hub.stop_all()
        for sca in self._scas.values():
            sca.close()

    def get_block(self, name: Optional[str] = None, timeout: Optional[float] = None) -> Optional[Block]:
        name = name or self._default_sensor_name()
        block = self.hub.get_block(name, timeout=timeout)
        if block is not None and self._write_to_disk:
            self._write_block(block)
        return block

    def health_status(self, name: Optional[str] = None) -> dict:
        if name is not None:
            return self.hub.health(name)
        if len(self._scas) == 1:
            return self.hub.health(self._default_sensor_name())
        return self.hub.health()  # aggregate: {sensor_name: status, ...}

    def _default_sensor_name(self) -> str:
        if len(self._scas) != 1:
            raise ValueError(
                f"{len(self._scas)} sensors registered ({self.sensor_names()}) -- specify which one by name"
            )
        return next(iter(self._scas))

    def _write_block(self, block: Block):
        fname = self._raw_dir / f"{block.sensor_name}_{block.t0_ns}.npz"
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
    sensor_names = acquirer.sensor_names()

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

    logger.info("Starting acquisition for sensors: %s", sensor_names)
    acquirer.start()

    health_interval = cfg.get("logging", {}).get("health_log_interval_s", 5)
    start_time = time.monotonic()
    last_health_log = start_time
    blocks_emitted = {name: 0 for name in sensor_names}

    try:
        while not stop_requested.is_set():
            if args.duration and (time.monotonic() - start_time) >= args.duration:
                break
            for name in sensor_names:
                block = acquirer.get_block(name, timeout=0.1)
                if block is not None:
                    blocks_emitted[name] += 1
                    logger.info("[%s] block %d ready: t0_ns=%d missed=%d",
                                name, blocks_emitted[name], block.t0_ns, block.missed_in_block)

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
