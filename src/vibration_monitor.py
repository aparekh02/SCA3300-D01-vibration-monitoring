#!/usr/bin/env python3
"""
vibration_monitor.py

Reads the SCA3300-D01 accelerometer over SPI on a Raspberry Pi and:
  - Logs raw X/Y/Z vibration data continuously to CSV
  - Runs FFT analysis on rolling windows of data, per-axis AND on the
    combined X/Y/Z vibration magnitude, to separate multiple simultaneous
    vibration sources (e.g. engine firing order vs. a bearing/pump tone)
  - Exports both raw samples and computed metrics to CSV
  - Computes a lightweight engine-room health score (0-100) that reacts
    both to sustained trend changes and to instantaneous spikes

Checklist covered:
  [x] Read accelerometer
  [x] Log raw vibration
  [x] FFT working (per-axis + combined multi-source breakdown)
  [x] Export CSV
  [x] Health metrics calculated (windowed trend + instant spike scoring)
"""

import spidev
import time
import csv
import os
import math
import numpy as np
from collections import deque
from pathlib import Path

from config import DualBandConfig, TrendConfig
from processing.dual_band import DualBandProcessor
from processing.trend import ExtendedBandTrendTracker

# ==================================================================
# SPI / Sensor setup (SCA3300-D01, per Murata datasheet Doc.No 3165)
# ==================================================================

spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 2000000   # 2 MHz, recommended range is 2-4 MHz
spi.mode = 0b00              # SPI Mode 0

def crc8(data24):
    """CRC-8 per datasheet Table 17 (poly 0x1D, init 0xFF, XOR-out 0xFF)."""
    crc = 0xFF
    for i in range(23, -1, -1):
        bit = (data24 >> i) & 0x01
        temp = crc & 0x80
        if bit == 0x01:
            temp ^= 0x80
        crc = (crc << 1) & 0xFF
        if temp > 0:
            crc ^= 0x1D
    return (~crc) & 0xFF

def build_frame(data24):
    c = crc8(data24)
    return list(data24.to_bytes(3, 'big') + bytes([c]))

SW_RESET     = [0xB4, 0x00, 0x20, 0x98]
CHANGE_MODE1 = [0xB4, 0x00, 0x00, 0x1F]
READ_STATUS  = [0x18, 0x00, 0x00, 0xE5]
READ_ACC_X   = [0x04, 0x00, 0x00, 0xF7]
READ_ACC_Y   = [0x08, 0x00, 0x00, 0xFD]
READ_ACC_Z   = [0x0C, 0x00, 0x00, 0xFB]

LSB_PER_G = 2700.0  # Mode 1 sensitivity, Table 12

def nop_frame():
    return build_frame(0x000000)

def xfer(cmd, label=None):
    r = spi.xfer2(list(cmd))
    if label:
        print(f"{label}: {[hex(b) for b in r]}")
    return r

def startup():
    xfer(SW_RESET, "SW_RESET")
    time.sleep(0.005)
    xfer(CHANGE_MODE1, "MODE1")
    time.sleep(0.02)
    xfer(READ_STATUS)
    xfer(READ_STATUS)
    r = xfer(READ_STATUS)
    rs = r[0] & 0x03
    print(f"RS after startup: {rs:02b} (expect 01)\n")
    return rs

def decode(r):
    raw = (r[1] << 8) | r[2]
    if raw & 0x8000:
        raw -= 65536
    return raw / LSB_PER_G

def read_axis(cmd):
    xfer(cmd)
    time.sleep(0.001)
    r = xfer(nop_frame())
    return decode(r)

def read_xyz():
    x = read_axis(READ_ACC_X)
    y = read_axis(READ_ACC_Y)
    z = read_axis(READ_ACC_Z)
    return x, y, z

# ==================================================================
# Configuration
# ==================================================================

# CONFIRM before relying on the dual-band extended path (see
# processing/dual_band.py): Nyquist here is SAMPLE_RATE_HZ/2 = 50 Hz, which
# is below both the extended band (70-82 Hz) and its noise-gate band
# (95-180 Hz) -- see NOTES.md. Raising this is the one change needed to
# make that path produce real (rather than safely-empty) output; it has
# not been raised here since the read_axis() timing budget (three
# hardcoded 1 ms settle sleeps per x/y/z sample = 3 ms floor) needs
# verifying against real hardware before assuming a higher rate is
# achievable.
SAMPLE_RATE_HZ   = 100          # target sampling rate (Hz)
WINDOW_SIZE      = 256          # samples per FFT window (power of 2 recommended)

BASE_DIR         = Path(__file__).resolve().parent.parent
RAW_LOG_FILE     = BASE_DIR / "data" / "raw" / "raw_vibration_log.csv"
METRICS_LOG_FILE = BASE_DIR / "data" / "metrics" / "vibration_metrics.csv"

SAMPLE_PERIOD = 1.0 / SAMPLE_RATE_HZ

# ==================================================================
# Dual-band vibration processor (additive -- see processing/dual_band.py,
# processing/trend.py, and NOTES.md/README.md for the two-path design and
# isolation guarantee)
# ==================================================================

DUAL_BAND_TRUSTED_LOG_FILE   = BASE_DIR / "data" / "metrics" / "dual_band_trusted.csv"
EXTENDED_BAND_TREND_LOG_FILE = BASE_DIR / "data" / "metrics" / "extended_band_trend.csv"

# fs is pinned to the loop's actual SAMPLE_RATE_HZ (not DualBandConfig's
# 2000.0 default) so this reflects what the live loop really captures.
DUAL_BAND_CONFIG = DualBandConfig(fs=float(SAMPLE_RATE_HZ))
TREND_CONFIG = TrendConfig()

# ==================================================================
# Calibration (baseline removal / deadband)
#
# Each axis carries a static offset (gravity component + mounting tilt)
# that would otherwise dominate the signal. Subtracting an adaptively
# tracked per-axis baseline isolates the vibration component so smaller
# amplitude events are still visible, while the deadband suppresses
# residual sensor/ADC noise around zero.
#
# The baseline adaptation is gated by activity: while an axis is
# actively vibrating, the baseline barely moves (ALPHA_ACTIVE), so it
# doesn't get dragged toward the vibration itself. That's what caused
# the old fixed-alpha filter to leave Z sitting non-zero for a while
# after real vibration stopped -- the baseline had chased the signal
# during the event and then had to slowly decay back afterward. While
# an axis is calm, the baseline snaps to it quickly (ALPHA_CALM) so
# genuine slow drift (temperature, mounting settling) is still tracked
# and idle axes read back to ~0 almost immediately.
# ==================================================================

DEADBAND_G = 0.02
ACTIVITY_THRESHOLD_G = 0.05   # |deviation| above this counts as "actively vibrating"

BASELINE_ALPHA_CALM = {
    'x': 0.05,
    'y': 0.05,
    'z': 0.15,   # faster for Z since it usually has bigger swings / settles further off
}
BASELINE_ALPHA_ACTIVE = {
    'x': 0.002,
    'y': 0.002,
    'z': 0.005,
}

baseline = {'x': 0.0, 'y': 0.0, 'z': 0.0}

def calibrate_baseline(num_samples=30, settle_delay=0.01):
    """Average a burst of still readings per axis to seed the baseline."""
    print("Calibrating baseline... keep sensor still for a moment")
    init_samples = {'x': [], 'y': [], 'z': []}
    for _ in range(num_samples):
        init_samples['x'].append(read_axis(READ_ACC_X))
        init_samples['y'].append(read_axis(READ_ACC_Y))
        init_samples['z'].append(read_axis(READ_ACC_Z))
        time.sleep(settle_delay)
    baseline['x'] = np.mean(init_samples['x'])
    baseline['y'] = np.mean(init_samples['y'])
    baseline['z'] = np.mean(init_samples['z'])
    print(f"Baseline -> X:{baseline['x']:+.3f}g Y:{baseline['y']:+.3f}g Z:{baseline['z']:+.3f}g\n")

def apply_calibration(axis, new_value):
    """Adapt the axis baseline (activity-gated) and return the deadbanded
    deviation from it."""
    deviation = new_value - baseline[axis]
    alpha = BASELINE_ALPHA_ACTIVE[axis] if abs(deviation) > ACTIVITY_THRESHOLD_G \
        else BASELINE_ALPHA_CALM[axis]
    baseline[axis] = (1 - alpha) * baseline[axis] + alpha * new_value
    deviation = new_value - baseline[axis]
    if abs(deviation) < DEADBAND_G:
        return 0.0
    return deviation

# ==================================================================
# CSV setup
# ==================================================================

METRICS_HEADER = [
    "window_end_time", "axis",
    "rms_g", "peak_g", "peak_to_peak_g", "crest_factor", "kurtosis",
    "peak1_freq_hz", "peak1_mag", "peak2_freq_hz", "peak2_mag",
    "peak3_freq_hz", "peak3_mag",
    "health_score", "health_status", "spike_in_window",
]

# Dual-band outputs are logged to their own files rather than added as
# columns to METRICS_HEADER, so that CSV schema stays exactly as it was.
# extended_band_trend.csv is the "clearly separate, flagged channel" the
# extended (UNCALIBRATED) path is routed to -- see README.md.
DUAL_BAND_TRUSTED_HEADER = [
    "window_end_time", "axis",
    "broadband_rms_g", "rms_0_10hz_g", "rms_10_30hz_g", "rms_30_70hz_g",
    "validated",
]

EXTENDED_BAND_TREND_HEADER = [
    "window_end_time", "axis", "rpm",
    "level", "reliable", "snr", "uncalibrated",
    "rising", "baseline",
]

def init_csv_files():
    RAW_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    METRICS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    raw_new = not RAW_LOG_FILE.exists()
    metrics_new = not METRICS_LOG_FILE.exists()
    dual_band_new = not DUAL_BAND_TRUSTED_LOG_FILE.exists()
    extended_trend_new = not EXTENDED_BAND_TREND_LOG_FILE.exists()

    raw_f = open(RAW_LOG_FILE, "a", newline="")
    raw_writer = csv.writer(raw_f)
    if raw_new:
        raw_writer.writerow(["timestamp", "x_g", "y_g", "z_g"])

    metrics_f = open(METRICS_LOG_FILE, "a", newline="")
    metrics_writer = csv.writer(metrics_f)
    if metrics_new:
        metrics_writer.writerow(METRICS_HEADER)

    dual_band_f = open(DUAL_BAND_TRUSTED_LOG_FILE, "a", newline="")
    dual_band_writer = csv.writer(dual_band_f)
    if dual_band_new:
        dual_band_writer.writerow(DUAL_BAND_TRUSTED_HEADER)

    extended_trend_f = open(EXTENDED_BAND_TREND_LOG_FILE, "a", newline="")
    extended_trend_writer = csv.writer(extended_trend_f)
    if extended_trend_new:
        extended_trend_writer.writerow(EXTENDED_BAND_TREND_HEADER)

    return (
        raw_f, raw_writer, metrics_f, metrics_writer,
        dual_band_f, dual_band_writer, extended_trend_f, extended_trend_writer,
    )

# ==================================================================
# FFT / spectral analysis
# ==================================================================

def compute_kurtosis(signal):
    """Excess kurtosis (0 = normal distribution baseline)."""
    signal = np.asarray(signal)
    mean = np.mean(signal)
    std = np.std(signal)
    if std == 0:
        return 0.0
    m4 = np.mean((signal - mean) ** 4)
    return (m4 / (std ** 4)) - 3.0

def compute_fft(signal, fs):
    """Returns (freqs, magnitudes) for the positive-frequency half of the spectrum."""
    n = len(signal)
    windowed = signal * np.hanning(n)          # reduce spectral leakage
    spectrum = np.fft.rfft(windowed)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    magnitudes = np.abs(spectrum) / n
    return freqs, magnitudes

def find_top_peaks(freqs, magnitudes, num_peaks=3, min_separation_hz=2.0, min_rel_mag=0.25,
                    min_prominence_ratio=0.5):
    """Greedy peak-pick the spectrum into up to num_peaks distinct frequency
    bands. On the combined X/Y/Z magnitude signal this is what separates
    multiple simultaneous vibration sources (e.g. shaft rotation vs. a
    bearing tone) instead of collapsing everything into one dominant bin.

    min_rel_mag (25% of the top bin) rejects bins that are just noise floor.
    On its own that's not enough to reject a single broadband/impulsive
    event (e.g. a knock or tap): its spectrum rolls off smoothly across many
    bins, so several bins spaced more than min_separation_hz apart can each
    still clear 25% of the peak even though they're all the same event's
    skirt, not competing tones. min_prominence_ratio fixes that: a candidate
    only counts as a distinct source if the spectrum actually dips -- below
    min_prominence_ratio times the smaller of the two -- somewhere between
    it and every peak already accepted. A real second tone has a valley
    separating it from the first; the flank of one broadband pulse doesn't."""
    if len(magnitudes) <= 1:
        return []
    search = magnitudes.copy()
    search[0] = 0.0  # ignore DC bin
    top = float(np.max(search))
    if top <= 0:
        return []
    bin_width = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
    sep_bins = max(1, int(round(min_separation_hz / bin_width)))

    masked = search.copy()
    accepted_idx = []
    peaks = []
    max_candidates = num_peaks * 4  # rejected shoulders don't consume a real "slot"

    for _ in range(max_candidates):
        if len(peaks) >= num_peaks:
            break
        idx = int(np.argmax(masked))
        mag = masked[idx]
        if mag <= 0 or mag < min_rel_mag * top:
            break

        is_distinct = True
        for a_idx in accepted_idx:
            lo, hi = sorted((idx, a_idx))
            valley = float(np.min(search[lo:hi + 1]))
            smaller = min(mag, search[a_idx])
            if valley > min_prominence_ratio * smaller:
                is_distinct = False
                break

        if is_distinct:
            peaks.append((float(freqs[idx]), float(mag)))
            accepted_idx.append(idx)

        lo = max(0, idx - sep_bins)
        hi = min(len(masked), idx + sep_bins + 1)
        masked[lo:hi] = 0.0
    return peaks

def compute_axis_metrics(signal, fs):
    """Per-axis RMS/peak/crest/kurtosis + single dominant frequency."""
    signal = np.asarray(signal)
    rms = float(np.sqrt(np.mean(signal ** 2)))
    peak = float(np.max(np.abs(signal)))
    p2p = float(np.max(signal) - np.min(signal))
    crest = float(peak / rms) if rms > 0 else 0.0
    kurt = float(compute_kurtosis(signal))
    freqs, mags = compute_fft(signal, fs)
    peaks = find_top_peaks(freqs, mags, num_peaks=1)
    dom_freq, dom_mag = peaks[0] if peaks else (0.0, 0.0)
    return {
        "rms": rms, "peak": peak, "p2p": p2p, "crest": crest, "kurtosis": kurt,
        "peaks": [(dom_freq, dom_mag)],
    }

def compute_combined_metrics(x_buf, y_buf, z_buf, fs):
    """Combine X/Y/Z into one vibration-magnitude signal and break its
    spectrum into multiple peaks to identify distinct vibration sources
    that no single axis would show cleanly on its own."""
    x = np.asarray(x_buf)
    y = np.asarray(y_buf)
    z = np.asarray(z_buf)
    magnitude = np.sqrt(x ** 2 + y ** 2 + z ** 2)

    rms = float(np.sqrt(np.mean(magnitude ** 2)))
    peak = float(np.max(magnitude))
    p2p = float(np.max(magnitude) - np.min(magnitude))
    crest = float(peak / rms) if rms > 0 else 0.0
    kurt = float(compute_kurtosis(magnitude))
    freqs, mags = compute_fft(magnitude, fs)
    peaks = find_top_peaks(freqs, mags, num_peaks=3)
    return {
        "rms": rms, "peak": peak, "p2p": p2p, "crest": crest, "kurtosis": kurt,
        "peaks": peaks,
    }

def metrics_row(window_end_time, axis, metrics, health_score, health_status, spike_in_window):
    peaks = list(metrics["peaks"]) + [(0.0, 0.0)] * (3 - len(metrics["peaks"]))
    row = [
        f"{window_end_time:.6f}", axis,
        f"{metrics['rms']:.4f}", f"{metrics['peak']:.4f}", f"{metrics['p2p']:.4f}",
        f"{metrics['crest']:.3f}", f"{metrics['kurtosis']:.3f}",
    ]
    for freq, mag in peaks[:3]:
        row += [f"{freq:.2f}", f"{mag:.5f}"]
    row += [f"{health_score:.1f}", health_status, int(spike_in_window)]
    return row

def dual_band_trusted_row(window_end_time, axis, trusted):
    sub = trusted.sub_band_rms
    return [
        f"{window_end_time:.6f}", axis,
        f"{trusted.broadband_rms:.5f}",
        f"{sub.get('0_10hz', 0.0):.5f}",
        f"{sub.get('10_30hz', 0.0):.5f}",
        f"{sub.get('30_70hz', 0.0):.5f}",
        int(trusted.validated),
    ]

def extended_band_trend_row(window_end_time, axis, rpm, extended, rising, baseline):
    return [
        f"{window_end_time:.6f}", axis,
        f"{rpm:.1f}" if rpm is not None else "",
        f"{extended.level:.5f}", int(extended.reliable), f"{extended.snr:.3f}",
        int(extended.uncalibrated), int(rising), f"{baseline:.5f}",
    ]

# ==================================================================
# Health scoring model
#
# Lightweight statistical model (no training/labeled data required, cheap
# enough to run on a Pi every sample). Two channels feed a single 0-100
# score:
#   - window trend: combined RMS vs. a slow-moving "normal" operating
#     level for this engine room. Sustained elevated vibration erodes
#     the score; genuinely calm windows slowly redefine "normal".
#   - instant spikes: every raw sample's combined magnitude is checked
#     against the recent short-term noise floor. A sudden, large jump
#     (by z-score AND absolute magnitude) drops the score immediately,
#     scaled by how far outside normal it is, without waiting for an
#     FFT window to fill -- this is what flags a sudden abnormal event
#     (e.g. impact, slip, bearing failure) in real time.
# The score recovers gradually while things stay calm.
# ==================================================================

HEALTH_MAX_SCORE = 100.0
HEALTH_RECOVERY_PER_SEC = 0.8

NORMAL_RMS_ALPHA = 0.01
WINDOW_EXCESS_CAP = 3.0
WINDOW_PENALTY_SCALE = 25.0

SPIKE_BUFFER_SECONDS = 1.0
SPIKE_MIN_SAMPLES = 20
SPIKE_Z_THRESHOLD = 5.0
SPIKE_MIN_MAGNITUDE_G = 0.15
SPIKE_BASE_PENALTY = 8.0
SPIKE_MAX_EXTRA_PENALTY = 22.0
SPIKE_MIN_STD_G = 0.01   # floor for the short-term std so a spike right after
                         # an unusually quiet patch (near-zero variance) still
                         # gets a well-defined, sane z-score instead of being
                         # skipped

class HealthMonitor:
    def __init__(self, sample_rate_hz):
        self.score = HEALTH_MAX_SCORE
        self.normal_rms = None
        self._recent_mag = deque(maxlen=int(sample_rate_hz * SPIKE_BUFFER_SECONDS))
        self._last_time = None

    def check_instant_sample(self, magnitude, now):
        """Per-sample spike check, independent of the FFT window boundary."""
        spike = None
        if len(self._recent_mag) >= SPIKE_MIN_SAMPLES:
            recent = np.fromiter(self._recent_mag, dtype=float)
            mean = recent.mean()
            std = max(float(recent.std()), SPIKE_MIN_STD_G)
            z = (magnitude - mean) / std
            if z >= SPIKE_Z_THRESHOLD and magnitude >= SPIKE_MIN_MAGNITUDE_G:
                severity = min(1.0, (z - SPIKE_Z_THRESHOLD) / SPIKE_Z_THRESHOLD)
                penalty = SPIKE_BASE_PENALTY + severity * SPIKE_MAX_EXTRA_PENALTY
                self.score = max(0.0, self.score - penalty)
                spike = {"z": float(z), "magnitude": float(magnitude), "penalty": float(penalty)}
        self._recent_mag.append(magnitude)
        self._apply_recovery(now)
        return spike

    def update_window(self, combined_rms, now):
        """Slower, window-level trend adjustment from the combined RMS level."""
        if self.normal_rms is None:
            self.normal_rms = combined_rms if combined_rms > 0 else 1e-6
            self._apply_recovery(now)
            return
        ratio = combined_rms / self.normal_rms
        excess = max(0.0, min(WINDOW_EXCESS_CAP, ratio - 1.0))
        if excess > 0:
            self.score = max(0.0, self.score - excess * WINDOW_PENALTY_SCALE / WINDOW_EXCESS_CAP)
        else:
            # only calm windows get to redefine "normal", so a sustained
            # problem doesn't quietly get accepted as the new baseline
            self.normal_rms = (1 - NORMAL_RMS_ALPHA) * self.normal_rms + NORMAL_RMS_ALPHA * combined_rms
        self._apply_recovery(now)

    def _apply_recovery(self, now):
        if self._last_time is not None and self.score < HEALTH_MAX_SCORE:
            dt = now - self._last_time
            self.score = min(HEALTH_MAX_SCORE, self.score + HEALTH_RECOVERY_PER_SEC * dt)
        self._last_time = now

    def status(self):
        if self.score >= 85:
            return "OK"
        if self.score >= 60:
            return "WARNING"
        if self.score >= 30:
            return "ABNORMAL - inspect"
        return "CRITICAL - fix needed"

# ==================================================================
# Main loop
# ==================================================================

def main():
    startup()
    calibrate_baseline()

    (
        raw_f, raw_writer, metrics_f, metrics_writer,
        dual_band_f, dual_band_writer, extended_trend_f, extended_trend_writer,
    ) = init_csv_files()
    health = HealthMonitor(SAMPLE_RATE_HZ)
    dual_band_processor = DualBandProcessor(DUAL_BAND_CONFIG)
    trend_tracker = ExtendedBandTrendTracker(TREND_CONFIG)

    # TODO(RPM-SOURCE): this repo has no CAN/tachometer input, so there is
    # no concurrent RPM value to pair with a block (see NOTES.md). The
    # trend tracker buckets by RPM (processing/trend.py), so until a real
    # source is wired in here, rpm stays None and the extended-band trend
    # update below is skipped -- extended_band_trend.csv still records
    # each block's UNCALIBRATED level/reliable/snr, just without a
    # baseline/rising judgement.
    rpm = None

    x_buf = deque(maxlen=WINDOW_SIZE)
    y_buf = deque(maxlen=WINDOW_SIZE)
    z_buf = deque(maxlen=WINDOW_SIZE)

    window_had_spike = False

    print(f"Sampling at {SAMPLE_RATE_HZ} Hz, FFT window = {WINDOW_SIZE} samples "
          f"(~{WINDOW_SIZE / SAMPLE_RATE_HZ:.1f}s per window)")
    print(f"Raw log     -> {RAW_LOG_FILE}")
    print(f"Metrics log -> {METRICS_LOG_FILE}")
    print(f"Dual-band trusted log -> {DUAL_BAND_TRUSTED_LOG_FILE}")
    print(f"Extended-band trend log (UNCALIBRATED, trend-only) -> {EXTENDED_BAND_TREND_LOG_FILE}")
    print("Press Ctrl+C to stop.\n")

    sample_count = 0
    next_sample_time = time.time()

    try:
        while True:
            now = time.time()
            raw_x, raw_y, raw_z = read_xyz()
            x = apply_calibration('x', raw_x)
            y = apply_calibration('y', raw_y)
            z = apply_calibration('z', raw_z)

            # ---- Log raw vibration ----
            raw_writer.writerow([f"{now:.6f}", f"{x:.4f}", f"{y:.4f}", f"{z:.4f}"])
            raw_f.flush()

            x_buf.append(x)
            y_buf.append(y)
            z_buf.append(z)
            sample_count += 1

            # ---- Instant spike check (independent of window boundary) ----
            magnitude = math.sqrt(x * x + y * y + z * z)
            spike = health.check_instant_sample(magnitude, now)
            if spike:
                window_had_spike = True
                print(f"[{now:.1f}] !! INSTANT SPIKE !! magnitude={spike['magnitude']:.3f}g "
                      f"(z={spike['z']:.1f}) -> health={health.score:.1f} "
                      f"({health.status()})")

            # ---- FFT + health metrics once every WINDOW_SIZE samples ----
            # (x_buf/y_buf/z_buf are maxlen deques, so their length stays at
            # WINDOW_SIZE forever once full -- gate on sample_count instead,
            # or this fires on every single sample instead of once per window)
            if len(x_buf) == WINDOW_SIZE and sample_count % WINDOW_SIZE == 0:
                combined = compute_combined_metrics(x_buf, y_buf, z_buf, SAMPLE_RATE_HZ)
                health.update_window(combined["rms"], now)
                score, status = health.score, health.status()

                for axis_name, buf in (("x", x_buf), ("y", y_buf), ("z", z_buf)):
                    m = compute_axis_metrics(np.array(buf), SAMPLE_RATE_HZ)
                    metrics_writer.writerow(
                        metrics_row(now, axis_name, m, score, status, window_had_spike)
                    )
                metrics_writer.writerow(
                    metrics_row(now, "combined", combined, score, status, window_had_spike)
                )
                metrics_f.flush()

                # ---- Dual-band vibration processor (additive) ----
                # Trusted: no correction, written to its own validated CSV
                # channel alongside the existing per-axis metrics above.
                # Extended: permanently UNCALIBRATED, routed ONLY to the
                # trend tracker / extended_band_trend.csv -- never into
                # `score`/`status` or metrics_writer above, so it cannot
                # reach the existing health scoring or any alarm/fault
                # logic. See README.md "Extended band usage rule".
                for axis_name, buf in (("x", x_buf), ("y", y_buf), ("z", z_buf)):
                    db_result = dual_band_processor.process(np.array(buf))
                    dual_band_writer.writerow(
                        dual_band_trusted_row(now, axis_name, db_result.trusted)
                    )

                    if rpm is not None:
                        rising, baseline = trend_tracker.update(rpm, db_result.extended)
                    else:
                        rising, baseline = False, 0.0
                    extended_trend_writer.writerow(
                        extended_band_trend_row(
                            now, axis_name, rpm, db_result.extended, rising, baseline
                        )
                    )
                dual_band_f.flush()
                extended_trend_f.flush()

                sources = ", ".join(f"{f:.1f}Hz" for f, _ in combined["peaks"]) or "none"
                print(f"[{now:.1f}] window processed | combined RMS={combined['rms']:.3f}g "
                      f"sources=[{sources}] | health={score:.1f} ({status})")

                window_had_spike = False

            # ---- Maintain target sample rate ----
            next_sample_time += SAMPLE_PERIOD
            sleep_time = next_sample_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                # We're behind schedule; resync instead of drifting further
                next_sample_time = time.time()

    except KeyboardInterrupt:
        print(f"\nStopped after {sample_count} samples.")
    finally:
        raw_f.close()
        metrics_f.close()
        dual_band_f.close()
        extended_trend_f.close()

if __name__ == "__main__":
    main()
