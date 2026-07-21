"""RPM-bucketed EMA baseline tracker for the extended band's `level`.
The only consumer of ExtendedBandResult -- never an alarm threshold or
fault logic, see README "Extended band usage rule"."""

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
    """Holds one EMA baseline + sample count per RPM bucket."""

    def __init__(self, config: TrendConfig = DEFAULT_TREND_CONFIG):
        self.config = config
        self._buckets: Dict[int, _BucketState] = {}

    def _bucket_key(self, rpm: float) -> int:
        return int(round(rpm / self.config.rpm_bucket_width))

    def update(self, rpm: float, extended: ExtendedBandResult) -> Tuple[bool, float]:
        """Returns (rising, baseline). Unreliable samples are ignored
        entirely: no baseline change, rising always False."""
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
