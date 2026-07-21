"""
Regression coverage for the existing (pre-dual-band) analysis in
vibration_monitor.py. There was no test suite for this module before this
change (see NOTES.md); these lock in current behavior of its pure
functions so the additive dual-band wiring can't silently alter them.
Hardware/IO (SPI reads, CSV files, the sampling loop) is out of scope --
only the parts of vibration_monitor.py that don't touch real hardware.
"""

import numpy as np
import pytest

import vibration_monitor as vm


def test_metrics_header_unchanged():
    assert vm.METRICS_HEADER == [
        "window_end_time", "axis",
        "rms_g", "peak_g", "peak_to_peak_g", "crest_factor", "kurtosis",
        "peak1_freq_hz", "peak1_mag", "peak2_freq_hz", "peak2_mag",
        "peak3_freq_hz", "peak3_mag",
        "health_score", "health_status", "spike_in_window",
    ]


def test_compute_kurtosis_constant_signal_is_zero():
    assert vm.compute_kurtosis(np.full(50, 0.5)) == 0.0


def test_compute_fft_finds_dominant_frequency():
    fs = 100.0
    n = 256
    t = np.arange(n) / fs
    freq = 10.0
    signal = 0.5 * np.sin(2 * np.pi * freq * t)

    freqs, mags = vm.compute_fft(signal, fs)
    peaks = vm.find_top_peaks(freqs, mags, num_peaks=1)

    assert len(peaks) == 1
    dom_freq, _ = peaks[0]
    assert dom_freq == pytest.approx(freq, abs=fs / n)


def test_compute_axis_metrics_rms_matches_definition():
    signal = np.array([1.0, -1.0, 1.0, -1.0])
    metrics = vm.compute_axis_metrics(signal, fs=100.0)

    assert metrics["rms"] == pytest.approx(1.0)
    assert metrics["peak"] == pytest.approx(1.0)
    assert metrics["p2p"] == pytest.approx(2.0)


def test_health_monitor_starts_at_max_score():
    health = vm.HealthMonitor(sample_rate_hz=100)
    assert health.score == vm.HEALTH_MAX_SCORE
    assert health.status() == "OK"
