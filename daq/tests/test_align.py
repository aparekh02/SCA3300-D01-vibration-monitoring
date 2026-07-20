#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from align import align_block, block_sample_times, interpolate_series


class TestBlockSampleTimes(unittest.TestCase):
    def test_evenly_spaced(self):
        times = block_sample_times(t0=10.0, n_samples=5, sample_rate_hz=2000.0)
        expected = np.array([10.0, 10.0005, 10.001, 10.0015, 10.002])
        np.testing.assert_allclose(times, expected)


class TestInterpolateSeries(unittest.TestCase):
    def test_linear_interpolation_matches_exact_values(self):
        series = [(0.0, 1000.0), (1.0, 1100.0), (2.0, 1200.0), (3.0, 1300.0)]
        result = interpolate_series(series, np.array([1.5]))
        self.assertAlmostEqual(result[0], 1150.0)

    def test_matches_at_known_sample_points(self):
        series = [(0.0, 1000.0), (1.0, 1100.0), (2.0, 1200.0), (3.0, 1300.0)]
        result = interpolate_series(series, np.array([0.0, 1.0, 2.0, 3.0]))
        np.testing.assert_allclose(result, [1000.0, 1100.0, 1200.0, 1300.0])

    def test_clamps_outside_range(self):
        series = [(1.0, 500.0), (2.0, 600.0)]
        result = interpolate_series(series, np.array([-5.0, 10.0]))
        np.testing.assert_allclose(result, [500.0, 600.0])

    def test_empty_series_returns_nan(self):
        result = interpolate_series([], np.array([0.0, 1.0]))
        self.assertTrue(np.all(np.isnan(result)))

    def test_unsorted_input_is_sorted_before_interpolation(self):
        series = [(2.0, 1200.0), (0.0, 1000.0), (1.0, 1100.0)]
        result = interpolate_series(series, np.array([0.5]))
        self.assertAlmostEqual(result[0], 1050.0)


class TestAlignBlock(unittest.TestCase):
    def test_align_block_end_to_end(self):
        # RPM ramps linearly from 1000 at t=0 to 2000 at t=10.
        series = [(t, 1000.0 + 100.0 * t) for t in range(11)]
        # A 2kHz, 8-sample block starting at t=5.0 spans [5.0, 5.0035].
        result = align_block(t0=5.0, n_samples=8, sample_rate_hz=2000.0, series=series)
        expected_times = block_sample_times(5.0, 8, 2000.0)
        expected = 1000.0 + 100.0 * expected_times
        np.testing.assert_allclose(result, expected)


if __name__ == "__main__":
    unittest.main()
