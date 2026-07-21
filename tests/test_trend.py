import pytest

from config import TrendConfig
from processing.dual_band import ExtendedBandResult
from processing.trend import ExtendedBandTrendTracker


def test_trend_baseline_builds_then_flags_rise_and_ignores_unreliable():
    cfg = TrendConfig(rpm_bucket_width=50.0, ema_alpha=0.05, min_samples=20, rise_ratio=1.5)
    tracker = ExtendedBandTrendTracker(cfg)
    rpm = 1500.0
    steady_level = 0.02

    rising = True
    baseline = None
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


def test_unreliable_before_any_reliable_sample_is_ignored():
    tracker = ExtendedBandTrendTracker()
    unreliable = ExtendedBandResult(level=1.0, reliable=False, snr=0.0)

    rising, baseline = tracker.update(1200.0, unreliable)

    assert rising is False
    assert baseline == 0.0


def test_buckets_are_independent_per_rpm():
    tracker = ExtendedBandTrendTracker(TrendConfig(rpm_bucket_width=50.0))
    low = ExtendedBandResult(level=0.01, reliable=True, snr=10.0)
    high = ExtendedBandResult(level=0.09, reliable=True, snr=10.0)

    _, baseline_low = tracker.update(1000.0, low)
    _, baseline_high = tracker.update(2000.0, high)

    assert baseline_low == pytest.approx(0.01)
    assert baseline_high == pytest.approx(0.09)
