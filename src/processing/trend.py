"""
trend.py

RPM-bucketed EMA baseline tracker for the extended band's `level`. This is
the ONLY consumer of ExtendedBandResult that vibration_monitor.py's
wiring feeds -- extended-band output must never reach an alarm threshold
or the order-tracking fault logic (it is permanently `uncalibrated`, a
relative trend signal only). See README.md "Extended band usage rule".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from config import TrendConfig, DEFAULT_TREND_CONFIG
from processing.dual_band import ExtendedBandResult


@dataclass
class _BucketState:
    baseline: float
    count: int


class ExtendedBandTrendTracker:
    """Stateful across calls (unlike DualBandProcessor): holds one EMA
    baseline + sample count per RPM bucket, keyed by rpm rounded to the
    nearest rpm_bucket_width."""

    def __init__(self, config: TrendConfig = DEFAULT_TREND_CONFIG):
        self.config = config
        self._buckets: Dict[int, _BucketState] = {}

    def _bucket_key(self, rpm: float) -> int:
        return int(round(rpm / self.config.rpm_bucket_width))

    def update(self, rpm: float, extended: ExtendedBandResult) -> Tuple[bool, float]:
        """Returns (rising, baseline) for this sample's bucket.

        Unreliable samples are ignored entirely: no baseline change, and
        `rising` is reported False rather than raising a flag off of data
        the extended path itself doesn't trust.
        """
        key = self._bucket_key(rpm)

        if not extended.reliable:
            existing = self._buckets.get(key)
            return False, existing.baseline if existing else 0.0

        cfg = self.config
        level = extended.level
        state = self._buckets.get(key)

        if state is None:
            self._buckets[key] = _BucketState(baseline=level, count=1)
            return False, level

        rising = (
            state.count >= cfg.min_samples
            and state.baseline > 0
            and level > state.baseline * cfg.rise_ratio
        )

        state.baseline = (1 - cfg.ema_alpha) * state.baseline + cfg.ema_alpha * level
        state.count += 1

        return rising, state.baseline
