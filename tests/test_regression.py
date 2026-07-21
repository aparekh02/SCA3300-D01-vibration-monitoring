"""Regression coverage for the pre-existing analysis in vibration_monitor.py
(no test suite existed before this change -- see NOTES.md). Hardware/IO is
out of scope; only pure functions are covered."""

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


def test_compute_fft_finds_dominant_frequency():
    fs, n, freq = 100.0, 256, 10.0
    t = np.arange(n) / fs
    signal = 0.5 * np.sin(2 * np.pi * freq * t)

    freqs, mags = vm.compute_fft(signal, fs)
    dom_freq, _ = vm.find_top_peaks(freqs, mags, num_peaks=1)[0]

    assert dom_freq == pytest.approx(freq, abs=fs / n)


def test_health_monitor_starts_at_max_score():
    health = vm.HealthMonitor(sample_rate_hz=100)
    assert health.score == vm.HEALTH_MAX_SCORE
    assert health.status() == "OK"
