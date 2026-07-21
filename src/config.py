"""
config.py

Configuration for the dual-band vibration processor (src/processing/).
The rest of vibration_monitor.py still uses its own top-of-file constants
(SAMPLE_RATE_HZ, WINDOW_SIZE, calibration/health tuning) -- this module is
additive and only covers the new dual-band/trend-tracker feature, so the
existing acquisition/analysis/health-scoring configuration is untouched.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DualBandConfig:
    """Tuning for DualBandProcessor (src/processing/dual_band.py).

    fc is the SCA3300's fixed first-order low-pass corner. 70.0 is the
    Mode-1 value called out in the Murata datasheet's frequency-response
    plot -- CONFIRM against the datasheet revision actually shipped with
    this sensor before relying on the extended-band correction in
    production; a wrong fc silently miscalibrates the de-emphasis gain.

    fs must be high enough that trusted_hi/ext_hi/noise_hi are all below
    the Nyquist frequency (fs/2), or those bands are simply absent from
    the FFT. See NOTES.md -- the live acquisition loop in
    vibration_monitor.py currently runs at 100 Hz (Nyquist 50 Hz), which
    cannot represent the 70-82 Hz extended band or the 95-180 Hz noise
    band at all. The 2000.0 default here matches the SCA3300's real
    sampling capability and is what the processor should be run at once
    the live loop's sample rate is raised and confirmed on real hardware;
    it is NOT the rate currently used by the running pipeline.
    """

    fs: float = 2000.0
    fc: float = 70.0  # CONFIRM-from-datasheet
    trusted_hi: float = 70.0
    ext_lo: float = 70.0
    ext_hi: float = 82.0
    gain_cap: float = 2.0
    noise_lo: float = 95.0
    noise_hi: float = 180.0
    snr_threshold: float = 3.0  # CONFIRM: tune against real baseline data
    detrend: bool = True


@dataclass(frozen=True)
class TrendConfig:
    """Tuning for ExtendedBandTrendTracker (src/processing/trend.py)."""

    rpm_bucket_width: float = 50.0  # CONFIRM: tune against real baseline data
    ema_alpha: float = 0.05
    min_samples: int = 20
    rise_ratio: float = 1.5  # CONFIRM: tune against real baseline data


DEFAULT_DUAL_BAND_CONFIG = DualBandConfig()
DEFAULT_TREND_CONFIG = TrendConfig()
