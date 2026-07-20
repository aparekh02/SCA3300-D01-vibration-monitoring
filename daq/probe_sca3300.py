#!/usr/bin/env python3
"""
probe_sca3300.py - standalone verification tool for the SCA3300-D01 link.

Run this BEFORE trusting acquire.py. It:
  1. Runs the datasheet startup sequence and confirms WHOAMI/STATUS.
  2. Reads a short burst and reports the CRC pass rate.
  3. Runs a gravity sanity check (device held still -> one axis ~+-1g, others ~0).
  4. Characterizes 2kHz read timing for a configurable duration (default 60s)
     and reports interval statistics, so we know whether the Pi can hold
     cadence or whether an MCU front-end is needed (see README fallback).

Writes probe_sca3300_result.json next to this script.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import yaml

from sca3300 import (
    SCA3300, SCA3300Error, MODE_TABLE, RS_ERROR,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("probe_sca3300")

HERE = Path(__file__).resolve().parent
GRAVITY_TOLERANCE_G = 0.15
TARGET_PERIOD_S = 0.5e-3  # 500us -> 2kHz


def load_config(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def gravity_check(sca: SCA3300, n_samples: int = 100, settle_s: float = 0.002) -> dict:
    xs, ys, zs = [], [], []
    for _ in range(n_samples):
        (x, y, z), rs, crc_ok = sca.read_accel()
        if crc_ok and rs != RS_ERROR:
            xs.append(x)
            ys.append(y)
            zs.append(z)
        time.sleep(settle_s)

    means = {"x": float(np.mean(xs)), "y": float(np.mean(ys)), "z": float(np.mean(zs))}
    axis_near_1g = {a: abs(abs(v) - 1.0) <= GRAVITY_TOLERANCE_G for a, v in means.items()}
    axes_at_1g = [a for a, ok in axis_near_1g.items() if ok]
    other_axes_near_0 = all(
        abs(v) <= GRAVITY_TOLERANCE_G for a, v in means.items() if a not in axes_at_1g
    )
    passed = len(axes_at_1g) == 1 and other_axes_near_0
    return {
        "n_samples": len(xs),
        "mean_g": means,
        "axis_reading_gravity": axes_at_1g[0] if len(axes_at_1g) == 1 else None,
        "passed": passed,
    }


def crc_burst_check(sca: SCA3300, n_frames: int = 500) -> dict:
    ok = 0
    rs_error_count = 0
    for _ in range(n_frames):
        try:
            _, rs, crc_ok = sca.read_accel()
        except SCA3300Error:
            continue
        if crc_ok:
            ok += 1
        if rs == RS_ERROR:
            rs_error_count += 1
    return {
        "frames_requested": n_frames,
        "frames_crc_ok": ok,
        "crc_pass_rate": ok / n_frames if n_frames else 0.0,
        "rs_error_count": rs_error_count,
    }


def timing_characterization(sca: SCA3300, duration_s: float) -> dict:
    """Read continuously at a target 2kHz for `duration_s`, driven by a
    monotonic fixed-rate timer (not by sensor interrupt/FIFO -- the -D01
    breakout has neither), and report inter-sample interval stats."""
    period_ns = int(TARGET_PERIOD_S * 1e9)
    tolerance_ns = int(period_ns * 0.05)

    intervals_ns = []
    crc_ok_count = 0
    rs_error_count = 0
    count = 0

    end_ns = time.monotonic_ns() + int(duration_s * 1e9)
    last_ns = time.monotonic_ns()
    next_tick_ns = last_ns + period_ns

    while time.monotonic_ns() < end_ns:
        try:
            _, rs, crc_ok = sca.read_accel()
            if crc_ok:
                crc_ok_count += 1
            if rs == RS_ERROR:
                rs_error_count += 1
        except SCA3300Error as exc:
            logger.warning("comms error during timing run: %s", exc)

        now_ns = time.monotonic_ns()
        intervals_ns.append(now_ns - last_ns)
        last_ns = now_ns
        count += 1

        next_tick_ns += period_ns
        remaining_ns = next_tick_ns - time.monotonic_ns()
        if remaining_ns > 0:
            time.sleep(remaining_ns / 1e9)
        else:
            next_tick_ns = time.monotonic_ns()

    intervals_us = [ns / 1000.0 for ns in intervals_ns[1:]]  # drop the first (no prior sample)
    missed = sum(1 for ns in intervals_ns[1:] if abs(ns - period_ns) > tolerance_ns)

    if not intervals_us:
        return {"count": 0, "error": "no samples collected"}

    arr = np.array(intervals_us)
    return {
        "count": count,
        "crc_pass_rate": crc_ok_count / count if count else 0.0,
        "rs_error_count": rs_error_count,
        "interval_us": {
            "mean": float(np.mean(arr)),
            "std": float(np.std(arr)),
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
            "p99": float(np.percentile(arr, 99)),
        },
        "target_interval_us": TARGET_PERIOD_S * 1e6,
        "missed_count": missed,
        "missed_pct": 100.0 * missed / len(intervals_us),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(HERE / "config.yaml"))
    parser.add_argument("--duration", type=float, default=None,
                         help="timing characterization duration in seconds (default from config, else 60)")
    parser.add_argument("--bus", type=int, default=None)
    parser.add_argument("--device", type=int, default=None)
    parser.add_argument("--speed", type=int, default=None)
    parser.add_argument("--mode", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    spi_cfg = cfg.get("spi", {})
    probe_cfg = cfg.get("probe", {})

    bus = args.bus if args.bus is not None else spi_cfg.get("bus", 0)
    device = args.device if args.device is not None else spi_cfg.get("device", 0)
    speed = args.speed if args.speed is not None else spi_cfg.get("max_speed_hz", 2_000_000)
    mode = args.mode if args.mode is not None else spi_cfg.get("mode", 1)
    duration = args.duration if args.duration is not None else probe_cfg.get("sca3300_duration_s", 60)

    result = {
        "spi": {"bus": bus, "device": device, "max_speed_hz": speed, "mode": mode},
        "timestamp": time.time(),
    }

    sca = SCA3300(bus=bus, device=device, max_speed_hz=speed, mode=mode)
    try:
        print(f"Opening SPI bus={bus} device={device} speed={speed}Hz mode={mode} ...")
        status = sca.start_up()
        whoami = sca.read_who_am_i()
        mode_info = MODE_TABLE[mode]

        print(f"WHOAMI       : 0x{whoami:02X} (expected 0x51)")
        print(f"STATUS       : raw=0x{status.raw_value:03X} rs={status.rs:02b} clean={status.clean}")
        print("STATUS bits  :", {k: v for k, v in status.bits.items() if v} or "none set")
        print(f"Mode         : {mode} ({mode_info['g_range']}, {mode_info['lpf_hz']}Hz LPF, "
              f"{mode_info['sensitivity_lsb_per_g']} LSB/g) "
              f"[{'confirmed' if mode_info['confirmed'] else 'UNCONFIRMED, see README'}]")

        result["whoami"] = whoami
        result["whoami_ok"] = whoami == 0x51
        result["status"] = {"raw_value": status.raw_value, "rs": status.rs, "clean": status.clean,
                             "bits": status.bits}
        result["mode_info"] = mode_info

        print("\nRunning short CRC burst check (500 frames)...")
        crc_result = crc_burst_check(sca)
        print(f"  CRC pass rate: {crc_result['crc_pass_rate']*100:.2f}% "
              f"({crc_result['frames_crc_ok']}/{crc_result['frames_requested']}) "
              f"RS errors: {crc_result['rs_error_count']}")
        result["crc_burst"] = crc_result

        print("\nGravity sanity check (hold sensor still)...")
        gravity_result = gravity_check(sca)
        print(f"  mean g: x={gravity_result['mean_g']['x']:+.3f} "
              f"y={gravity_result['mean_g']['y']:+.3f} z={gravity_result['mean_g']['z']:+.3f}")
        print(f"  axis reading ~1g: {gravity_result['axis_reading_gravity']} "
              f"-> {'PASS' if gravity_result['passed'] else 'FAIL'}")
        result["gravity_check"] = gravity_result

        print(f"\nTiming characterization: target 2kHz for {duration:.0f}s ...")
        timing_result = timing_characterization(sca, duration)
        interval = timing_result.get("interval_us", {})
        print(f"  samples={timing_result.get('count')} "
              f"mean={interval.get('mean', float('nan')):.1f}us "
              f"std={interval.get('std', float('nan')):.1f}us "
              f"min={interval.get('min', float('nan')):.1f}us "
              f"max={interval.get('max', float('nan')):.1f}us "
              f"p99={interval.get('p99', float('nan')):.1f}us")
        print(f"  missed (outside +-5% of 500us): {timing_result.get('missed_count')} "
              f"({timing_result.get('missed_pct', 0):.2f}%)")
        result["timing"] = timing_result

        p99 = interval.get("p99")
        target = timing_result.get("target_interval_us", 500.0)
        holds_cadence = (
            timing_result.get("missed_count", 1) == 0
            and p99 is not None and abs(p99 - target) <= 0.05 * target
        )
        result["holds_2khz_cadence"] = holds_cadence
        print(f"\nHolds 2kHz cadence within +-5% p99: {'YES' if holds_cadence else 'NO -- see README MCU fallback'}")

    except SCA3300Error as exc:
        logger.error("SCA3300 error: %s", exc)
        result["error"] = str(exc)
        sca.close()
        out_path = HERE / "probe_sca3300_result.json"
        out_path.write_text(json.dumps(result, indent=2))
        sys.exit(1)
    finally:
        sca.close()

    out_path = HERE / "probe_sca3300_result.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
