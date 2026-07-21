"""Config for the dual-band vibration processor (src/processing/).
vibration_monitor.py's own constants (SAMPLE_RATE_HZ, WINDOW_SIZE, etc.)
are untouched -- this only covers the new dual-band/trend-tracker feature.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DualBandConfig:
    """Tuning for DualBandProcessor. fs must be high enough that
    ext_hi/noise_hi are below Nyquist (fs/2) or those bands are absent
    from the FFT -- see NOTES.md."""

    fs: float = 2000.0
    fc: float = 70.0  # CONFIRM-from-datasheet: SCA3300 Mode-1 LPF corner
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
    """Tuning for ExtendedBandTrendTracker."""

    rpm_bucket_width: float = 50.0  # CONFIRM: tune against real baseline data
    ema_alpha: float = 0.05
    min_samples: int = 20
    rise_ratio: float = 1.5  # CONFIRM: tune against real baseline data


DEFAULT_DUAL_BAND_CONFIG = DualBandConfig()
DEFAULT_TREND_CONFIG = TrendConfig()
