#!/usr/bin/env python3
"""
watch_rpm.py - manual can_map.yaml confirmation helper.

can_discover.py can surface RPM/load/torque *candidates*, but only a human
watching a real spin-up or throttle change can actually confirm one is
correct -- that's not something a script can assert its way past. Run this
while changing engine speed/throttle and watch whether the printed values
track reality; once you're confident, flip `confirmed: true` for that
signal in can_map.yaml (see HARDWARE_TESTING.md).

Usage:
    python3 tests/hardware/watch_rpm.py --duration 60
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import yaml

from can_reader import CanReader, CanMapError

DAQ_DIR = Path(__file__).resolve().parent.parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DAQ_DIR / "config.yaml"))
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=0.5, help="print interval in seconds")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    can_cfg = cfg["can"]
    map_path = DAQ_DIR / can_cfg.get("map_file", "can_map.yaml")

    try:
        reader = CanReader(channel=can_cfg["channel"], bustype=can_cfg["bustype"],
                            bitrate=can_cfg["bitrate"], can_map_path=map_path,
                            gap_timeout_s=can_cfg.get("gap_timeout_s", 1.0))
    except CanMapError as exc:
        print(f"error: {exc}")
        sys.exit(1)

    if not reader.signals:
        print(f"no confirmed:true signals in {map_path} -- nothing to watch. "
              f"Edit can_map.todo.yaml/can_map.yaml first.")
        sys.exit(1)

    reader.start()
    print(f"Watching {list(reader.signals.keys())} for {args.duration:.0f}s.")
    print("Rev the engine / change throttle now and confirm the values track reality.")
    print("Press Ctrl+C to stop early.\n")

    end = time.monotonic() + args.duration
    try:
        while time.monotonic() < end:
            row = []
            for name in reader.signals:
                series = reader.series_snapshot(name)
                latest = f"{series[-1][1]:.2f}" if series else "no data yet"
                row.append(f"{name}={latest}")
            print(f"[{time.strftime('%H:%M:%S')}] " + " | ".join(row))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        reader.stop()
        if reader.gap_count:
            print(f"\n{reader.gap_count} bus gap(s) (no message for >= "
                  f"{can_cfg.get('gap_timeout_s', 1.0)}s) occurred during the watch")


if __name__ == "__main__":
    main()
