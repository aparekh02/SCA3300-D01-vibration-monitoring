#!/usr/bin/env python3
"""
can_discover.py - CAN adapter/bus discovery and verification tool.

Run this BEFORE trusting can_reader.py / acquire.py's RPM correlation. It:
  1. Detects the adapter driver (native SocketCAN e.g. gs_usb/candleLight vs.
     slcan) and whether kernel timestamping is available.
  2. Sniffs the bus for a configurable window (default 60s) and tables every
     arbitration ID seen: 11- vs 29-bit, rate, sample payloads.
  3. Decodes J1939 PGN/source-address for 29-bit IDs and flags likely RPM /
     load / torque candidates (EEC1 SPN190, SPN513; EEC2 SPN92) -- flagged,
     never silently assumed.
  4. Checks that timestamps are monotonic and reports their resolution.

Writes can_discover_result.json and a regenerated can_map.todo.yaml (do not
promote entries to can_map.yaml without confirming them against a real
spin-up/throttle change).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import yaml

import can

from j1939 import decode_29bit_id, decode_signal, KNOWN_SIGNALS, PGN_EEC1, PGN_EEC2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("can_discover")

HERE = Path(__file__).resolve().parent


def load_config(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _run(cmd: list) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("command %s failed: %s", cmd, exc)
        return ""


def detect_adapter(iface: str) -> dict:
    """Best-effort adapter/driver detection via standard Linux tooling.
    SocketCAN devices are not ethtool-compatible net devices in the usual
    sense, so this leans on `ip -details link show` and the sysfs driver
    symlink first, with dmesg as a corroborating signal."""
    info = {
        "iface": iface,
        "driver": "unknown",
        "kind": "unknown",  # "native_socketcan" | "slcan" | "unknown"
        "kernel_timestamping_expected": None,
        "evidence": [],
    }

    ip_out = _run(["ip", "-details", "link", "show", iface])
    if ip_out:
        info["evidence"].append({"source": "ip -details link show", "text": ip_out.strip()})
        m = re.search(r"gs_usb|candleLight|candlelight", ip_out, re.IGNORECASE)
        if m:
            info["driver"] = "gs_usb"
            info["kind"] = "native_socketcan"

    driver_link = Path(f"/sys/class/net/{iface}/device/driver")
    if driver_link.exists():
        try:
            resolved = driver_link.resolve().name
            info["evidence"].append({"source": "sysfs driver symlink", "text": resolved})
            if info["driver"] == "unknown":
                info["driver"] = resolved
            if "gs_usb" in resolved:
                info["kind"] = "native_socketcan"
        except OSError as exc:
            logger.debug("could not resolve driver symlink: %s", exc)
    else:
        # slcan interfaces are a tty line discipline, not a USB netdev, so
        # this symlink typically won't exist for them.
        info["evidence"].append({"source": "sysfs driver symlink", "text": "absent (consistent with slcan)"})

    dmesg_out = _run(["dmesg"])
    if dmesg_out:
        if re.search(rf"slcan.*{re.escape(iface)}|{re.escape(iface)}.*slcan", dmesg_out, re.IGNORECASE):
            if info["kind"] == "unknown":
                info["kind"] = "slcan"
                info["driver"] = "slcan"
            info["evidence"].append({"source": "dmesg", "text": "slcan reference found"})
        if re.search(r"gs_usb", dmesg_out, re.IGNORECASE):
            info["evidence"].append({"source": "dmesg", "text": "gs_usb reference found"})

    if iface.startswith("slcan"):
        info["kind"] = "slcan" if info["kind"] == "unknown" else info["kind"]
        info["driver"] = info["driver"] if info["driver"] != "unknown" else "slcan"

    info["kernel_timestamping_expected"] = info["kind"] == "native_socketcan"
    return info


def sniff_bus(iface: str, bustype: str, bitrate: int, duration_s: float) -> dict:
    bus = can.interface.Bus(channel=iface, interface=bustype, bitrate=bitrate)
    seen = defaultdict(lambda: {"count": 0, "is_extended": None, "timestamps": [], "payloads": []})

    end_time = time.monotonic() + duration_s
    try:
        while time.monotonic() < end_time:
            remaining = end_time - time.monotonic()
            msg = bus.recv(timeout=min(1.0, max(0.0, remaining)))
            if msg is None:
                continue
            entry = seen[msg.arbitration_id]
            entry["count"] += 1
            entry["is_extended"] = msg.is_extended_id
            entry["timestamps"].append(msg.timestamp)
            if len(entry["payloads"]) < 3:
                entry["payloads"].append(list(msg.data))
    finally:
        bus.shutdown()

    return dict(seen)


def analyze(seen: dict, duration_s: float) -> dict:
    ids_table = []
    candidates = []
    all_deltas = []

    for can_id, entry in seen.items():
        rate_hz = entry["count"] / duration_s if duration_s > 0 else 0.0
        ts = entry["timestamps"]
        deltas = [b - a for a, b in zip(ts, ts[1:])]
        monotonic = all(d >= 0 for d in deltas)
        all_deltas.extend(d for d in deltas if d > 0)

        row = {
            "can_id": can_id,
            "can_id_hex": f"0x{can_id:X}",
            "is_extended": entry["is_extended"],
            "count": entry["count"],
            "rate_hz": rate_hz,
            "sample_payloads_hex": [bytes(p).hex() for p in entry["payloads"]],
            "timestamps_monotonic": monotonic,
        }

        if entry["is_extended"]:
            j1939 = decode_29bit_id(can_id)
            row["j1939"] = {
                "pgn": j1939.pgn,
                "priority": j1939.priority,
                "source_address": j1939.source_address,
                "is_peer_to_peer": j1939.is_peer_to_peer,
            }
            if j1939.pgn in (PGN_EEC1, PGN_EEC2) and entry["payloads"]:
                for (pgn, spn), (name, byte_off, length, order, res, off, unit) in KNOWN_SIGNALS.items():
                    if pgn != j1939.pgn:
                        continue
                    try:
                        sample_value = decode_signal(bytes(entry["payloads"][0]), byte_off, length, res, off, order)
                    except ValueError:
                        sample_value = None
                    candidates.append({
                        "can_id_hex": row["can_id_hex"],
                        "pgn": pgn,
                        "spn": spn,
                        "name": name,
                        "unit": unit,
                        "source_address": j1939.source_address,
                        "rate_hz": rate_hz,
                        "sample_decoded_value": sample_value,
                        "byte_offset": byte_off,
                        "length_bytes": length,
                        "byte_order": order,
                        "resolution": res,
                        "offset": off,
                    })

        ids_table.append(row)

    ids_table.sort(key=lambda r: -r["rate_hz"])

    resolution = min(all_deltas) if all_deltas else None
    all_monotonic = all(r["timestamps_monotonic"] for r in ids_table)

    return {
        "ids": ids_table,
        "candidates": candidates,
        "timestamps_all_monotonic": all_monotonic,
        "timestamp_resolution_s": resolution,
    }


def write_can_map_todo(candidates: list, path: Path):
    signals = {}
    for c in candidates:
        key = c["name"]
        if key in signals:
            continue  # keep the first (highest-rate, since candidates list isn't pre-sorted globally; fine as a starting point)
        signals[key] = {
            "protocol": "j1939",
            "pgn": c["pgn"],
            "spn": c["spn"],
            "byte_offset": c["byte_offset"],
            "length_bytes": c["length_bytes"],
            "byte_order": c["byte_order"],
            "resolution": c["resolution"],
            "offset": c["offset"],
            "source_address": c["source_address"],
            "candidate_can_id_hex": c["can_id_hex"],
            "observed_rate_hz": round(c["rate_hz"], 2),
            "sample_decoded_value": c["sample_decoded_value"],
            "confirmed": False,
        }

    doc = {
        "schema_version": 1,
        "generated_by": "can_discover.py",
        "generated_at": time.time(),
        "note": (
            "Auto-generated from a live bus sniff. Confirm each candidate against a "
            "real spin-up/throttle change, then copy confirmed entries into can_map.yaml."
        ),
        "signals": signals,
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(HERE / "config.yaml"))
    parser.add_argument("--iface", default=None)
    parser.add_argument("--bustype", default=None, help="python-can interface backend, e.g. socketcan or slcan")
    parser.add_argument("--bitrate", type=int, default=None)
    parser.add_argument("--duration", type=float, default=None)
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    can_cfg = cfg.get("can", {})
    probe_cfg = cfg.get("probe", {})

    iface = args.iface or can_cfg.get("channel", "can0")
    bustype = args.bustype or can_cfg.get("bustype", "socketcan")
    bitrate = args.bitrate or can_cfg.get("bitrate", 250000)
    duration = args.duration or probe_cfg.get("can_discover_duration_s", 60)

    print(f"Detecting adapter for {iface} ...")
    adapter_info = detect_adapter(iface)
    print(f"  driver={adapter_info['driver']} kind={adapter_info['kind']} "
          f"kernel_timestamping_expected={adapter_info['kernel_timestamping_expected']}")
    if adapter_info["kind"] == "unknown":
        print("  WARNING: could not determine adapter type automatically -- "
              "check `ip -details link show` output in the result JSON manually.")

    print(f"\nSniffing {iface} ({bustype} @ {bitrate}bps) for {duration:.0f}s ...")
    try:
        seen = sniff_bus(iface, bustype, bitrate, duration)
    except can.CanError as exc:
        logger.error("failed to open/sniff CAN bus: %s", exc)
        result = {"adapter": adapter_info, "error": str(exc)}
        (HERE / "can_discover_result.json").write_text(json.dumps(result, indent=2))
        raise SystemExit(1)

    analysis = analyze(seen, duration)

    print(f"\n{'CAN ID':<12}{'ext':<5}{'rate(Hz)':<10}{'PGN':<8}{'SA':<5}samples")
    for row in analysis["ids"]:
        pgn = row.get("j1939", {}).get("pgn", "")
        sa = row.get("j1939", {}).get("source_address", "")
        print(f"{row['can_id_hex']:<12}{str(row['is_extended']):<5}{row['rate_hz']:<10.2f}"
              f"{str(pgn):<8}{str(sa):<5}{row['sample_payloads_hex'][:2]}")

    print(f"\nTimestamps monotonic: {analysis['timestamps_all_monotonic']}")
    print(f"Observed timestamp resolution: {analysis['timestamp_resolution_s']}")

    if analysis["candidates"]:
        print("\nCandidate RPM/load/torque signals (NOT confirmed -- edit can_map.todo.yaml):")
        for c in analysis["candidates"]:
            print(f"  {c['name']:<35} PGN={c['pgn']} SPN={c['spn']} id={c['can_id_hex']} "
                  f"rate={c['rate_hz']:.2f}Hz sample={c['sample_decoded_value']}")
    else:
        print("\nNo EEC1/EEC2 J1939 candidates observed in this window -- "
              "either the bus is proprietary (edit can_map.todo.yaml manually) "
              "or the relevant messages weren't seen during this sniff.")

    result = {"adapter": adapter_info, "analysis": analysis, "config": {
        "iface": iface, "bustype": bustype, "bitrate": bitrate, "duration_s": duration,
    }}
    out_path = HERE / "can_discover_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"\nWrote {out_path}")

    todo_path = HERE / "can_map.todo.yaml"
    write_can_map_todo(analysis["candidates"], todo_path)
    print(f"Wrote {todo_path} (regenerated from this sniff)")


if __name__ == "__main__":
    main()
