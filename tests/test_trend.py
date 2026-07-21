import pytest

from config import TrendConfig
from processing.dual_band import ExtendedBandResult
from processing.trend import ExtendedBandTrendTracker


def test_trend_baseline_builds_then_flags_rise_and_ignores_unreliable():
    tracker = ExtendedBandTrendTracker(TrendConfig(min_samples=20, rise_ratio=1.5))
    rpm = 1500.0
    steady_level = 0.02

    for _ in range(40):
        steady = ExtendedBandResult(level=steady_level, reliable=True, snr=10.0)
        rising, baseline = tracker.update(rpm, steady)
    assert rising is False
    assert baseline == pytest.approx(steady_level, rel=0.3)

    spike = ExtendedBandResult(level=steady_level * 2.0, reliable=True, snr=10.0)
    rising, baseline_after_spike = tracker.update(rpm, spike)
    assert rising is True

    unreliable = ExtendedBandResult(level=999.0, reliable=False, snr=0.1)
    rising_unreliable, baseline_unchanged = tracker.update(rpm, unreliable)
    assert rising_unreliable is False
    assert baseline_unchanged == baseline_after_spike
