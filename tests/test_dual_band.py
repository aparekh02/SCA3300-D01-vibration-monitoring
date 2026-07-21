"""Dual-band processor tests: recovery / noise-rejection / isolation.

Test frequencies land on exact FFT bins (k * fs/N) instead of literal
"25 Hz"/"78 Hz" to avoid leakage from non-integer-cycle tones making
assertions flaky.
"""

import numpy as np
import pytest

from config import DualBandConfig
from processing.dual_band import DualBandProcessor

FS = 2000.0
N = 4096
FC = 70.0
DF = FS / N  # ~0.488 Hz per bin

TRUSTED_TONE_HZ = 51 * DF  # ~24.9 Hz, inside the trusted (<=70 Hz) band
EXT_TONE_HZ = 160 * DF  # ~78.1 Hz, inside the extended (70-82 Hz) band


def _sinusoid_rms(freq_hz, rms, n=N, fs=FS, phase=0.3):
    t = np.arange(n) / fs
    amplitude = rms * np.sqrt(2.0)
    return amplitude * np.sin(2 * np.pi * freq_hz * t + phase)


def test_recovery_extended_band_matches_true_rms_and_trusted_isolated():
    """References are computed through the *same* band_rms/level pipeline
    (isolated pure tones) rather than an idealized closed-form RMS, since
    the specified band_rms formula carries a fixed Hann-window
    normalization constant that cancels out of same-pipeline comparisons
    but not against a textbook RMS value."""
    rng = np.random.default_rng(0)
    cfg = DualBandConfig(fs=FS, fc=FC)
    processor = DualBandProcessor(cfg)

    trusted_rms = 0.10
    ext_true_rms = 0.05
    atten = 1.0 / np.sqrt(1.0 + (EXT_TONE_HZ / FC) ** 2)
    ext_injected_rms = ext_true_rms * atten

    signal = (
        _sinusoid_rms(TRUSTED_TONE_HZ, trusted_rms, phase=0.1)
        + _sinusoid_rms(EXT_TONE_HZ, ext_injected_rms, phase=1.7)
        + rng.normal(scale=0.0005, size=N)
    )
    result = processor.process(signal)

    reference_trusted_rms = processor.process(
        _sinusoid_rms(TRUSTED_TONE_HZ, trusted_rms, phase=0.1)
    ).trusted.broadband_rms

    # fc effectively infinite -> gain(f ~78Hz) ~= 1, so this measures the
    # *true* (unattenuated) ext tone through the identical amp/S1 pipeline,
    # with no de-emphasis correction applied.
    reference_cfg = DualBandConfig(fs=FS, fc=1.0e9)
    reference_ext_level = (
        DualBandProcessor(reference_cfg)
        .process(_sinusoid_rms(EXT_TONE_HZ, ext_true_rms, phase=1.7))
        .extended.level
    )

    assert result.trusted.validated is True
    assert result.trusted.broadband_rms == pytest.approx(reference_trusted_rms, rel=0.05)

    assert result.extended.uncalibrated is True
    assert result.extended.reliable is True
    assert result.extended.level == pytest.approx(reference_ext_level, rel=0.1)


def test_noise_only_extended_band_is_unreliable():
    rng = np.random.default_rng(1)

    trusted_rms = 0.10
    signal = _sinusoid_rms(TRUSTED_TONE_HZ, trusted_rms) + rng.normal(
        scale=0.01, size=N
    )

    cfg = DualBandConfig(fs=FS, fc=FC)
    result = DualBandProcessor(cfg).process(signal)

    assert result.extended.reliable is False


def test_trusted_rms_isolated_from_high_band_content():
    rng = np.random.default_rng(2)
    trusted_rms = 0.10

    without_high_band = _sinusoid_rms(
        TRUSTED_TONE_HZ, trusted_rms, phase=0.1
    ) + rng.normal(scale=0.0005, size=N)

    rng2 = np.random.default_rng(2)
    strong_high_band = _sinusoid_rms(EXT_TONE_HZ, 5.0, phase=1.7)
    with_high_band = (
        _sinusoid_rms(TRUSTED_TONE_HZ, trusted_rms, phase=0.1)
        + strong_high_band
        + rng2.normal(scale=0.0005, size=N)
    )

    cfg = DualBandConfig(fs=FS, fc=FC)
    processor = DualBandProcessor(cfg)

    rms_without = processor.process(without_high_band).trusted.broadband_rms
    rms_with = processor.process(with_high_band).trusted.broadband_rms

    assert rms_with == pytest.approx(rms_without, rel=0.02)
