#!/usr/bin/env python3
"""Run ON THE PI with the SCA3300 connected (imports vibration_monitor.py,
which opens the real SPI bus at import time) to check what this repo can't
verify without hardware: whether read_axis()/read_xyz() actually sustain
SAMPLE_RATE_HZ, and whether a live block shows content above 70 Hz through
both the existing general FFT and the dual-band processor. See NOTES.md
Section 3.

Usage: python3 src/dual_band_hardware_check.py [--seconds 5]
"""

import argparse
import time

import numpy as np

import vibration_monitor as vm
from processing.dual_band import DualBandProcessor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds", type=float, default=5.0,
        help="How long to sample for timing statistics (default 5s). "
             "The most recent WINDOW_SIZE samples are used for the "
             "FFT / dual-band comparison.",
    )
    args = parser.parse_args()

    print(f"Target SAMPLE_RATE_HZ={vm.SAMPLE_RATE_HZ} "
          f"(period={vm.SAMPLE_PERIOD * 1000:.2f} ms), "
          f"WINDOW_SIZE={vm.WINDOW_SIZE} "
          f"(~{vm.WINDOW_SIZE / vm.SAMPLE_RATE_HZ:.2f}s/window)")
    print(f"DualBandConfig in use: {vm.DUAL_BAND_CONFIG}\n")

    vm.startup()
    vm.calibrate_baseline()

    n = max(vm.WINDOW_SIZE, int(args.seconds * vm.SAMPLE_RATE_HZ))
    x = np.zeros(n)
    y = np.zeros(n)
    z = np.zeros(n)
    dt = np.zeros(n)

    print(f"Sampling {n} x/y/z reads...")
    t_prev = time.time()
    for i in range(n):
        raw_x, raw_y, raw_z = vm.read_xyz()
        x[i] = vm.apply_calibration('x', raw_x)
        y[i] = vm.apply_calibration('y', raw_y)
        z[i] = vm.apply_calibration('z', raw_z)
        t_now = time.time()
        dt[i] = t_now - t_prev
        t_prev = t_now

    elapsed = float(dt.sum())
    achieved_hz = n / elapsed if elapsed > 0 else 0.0
    mean_dt = float(dt[1:].mean())
    jitter = float(dt[1:].std())

    print("\n--- Timing ---")
    print(f"Achieved rate: {achieved_hz:.1f} Hz over {elapsed:.2f}s "
          f"(target {vm.SAMPLE_RATE_HZ} Hz)")
    print(f"Mean inter-sample dt: {mean_dt * 1000:.3f} ms  "
          f"jitter (std): {jitter * 1000:.3f} ms")
    if achieved_hz < 0.9 * vm.SAMPLE_RATE_HZ:
        print("WARNING: achieved rate is >10% below target -- SAMPLE_RATE_HZ "
              "is likely too high for this loop's timing budget on this "
              "hardware. Lower it (src/vibration_monitor.py) before trusting "
              "window timing or the extended band -- see NOTES.md Section 3.")
    else:
        print("OK: achieved rate is within 10% of target.")

    block_x, block_y, block_z = x[-vm.WINDOW_SIZE:], y[-vm.WINDOW_SIZE:], z[-vm.WINDOW_SIZE:]
    nyquist = vm.SAMPLE_RATE_HZ / 2.0

    print(f"\n--- Existing general FFT (compute_fft / find_top_peaks, unchanged) "
          f"-- last {vm.WINDOW_SIZE} samples, Nyquist={nyquist:.1f} Hz ---")
    for axis_name, buf in (("x", block_x), ("y", block_y), ("z", block_z)):
        freqs, mags = vm.compute_fft(buf, vm.SAMPLE_RATE_HZ)
        peaks = vm.find_top_peaks(freqs, mags, num_peaks=3)
        n_above_70 = int(np.sum(freqs > 70.0))
        peak_str = ", ".join(f"{f:.1f}Hz" for f, _ in peaks) or "none"
        print(f"[{axis_name}] top peaks: {peak_str} | "
              f"bins above 70 Hz: {n_above_70} (spectrum extends to {freqs.max():.1f} Hz)")
        if n_above_70 == 0:
            print("    NO bins above 70 Hz -- Nyquist is at or below 70 Hz, "
                  "raise SAMPLE_RATE_HZ further.")

    print("\n--- Dual-band processor (separate computation, same live block) ---")
    processor = DualBandProcessor(vm.DUAL_BAND_CONFIG)
    for axis_name, buf in (("x", block_x), ("y", block_y), ("z", block_z)):
        result = processor.process(buf)
        t, e = result.trusted, result.extended
        print(f"[{axis_name}] trusted: broadband_rms={t.broadband_rms:.4f}g "
              f"validated={t.validated} | "
              f"extended: level={e.level:.4f} reliable={e.reliable} "
              f"snr={e.snr:.2f} uncalibrated={e.uncalibrated}")

    print("\nDone. See NOTES.md Section 3 for how to read these numbers.")


if __name__ == "__main__":
    main()
