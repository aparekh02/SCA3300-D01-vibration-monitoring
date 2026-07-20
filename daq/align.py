#!/usr/bin/env python3
"""
align.py - linear interpolation of a CAN time-series onto a vibration
block's sample times. This is only the alignment seam Task 2 builds and
tests; FFT/diagnostics that consume the aligned RPM are a later task.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np


def block_sample_times(t0: float, n_samples: int, sample_rate_hz: float) -> np.ndarray:
    """Per-sample times for a block: t0 + i/sample_rate_hz."""
    return t0 + np.arange(n_samples) / sample_rate_hz


def interpolate_series(series: Sequence[tuple], query_times: np.ndarray) -> np.ndarray:
    """Linearly interpolate a (time, value) series onto query_times.
    Times outside the series' range are clamped to the nearest endpoint
    (not extrapolated). Returns all-NaN if `series` is empty."""
    query_times = np.asarray(query_times, dtype=float)
    if not series:
        return np.full(query_times.shape, np.nan)

    times = np.array([t for t, _ in series], dtype=float)
    values = np.array([v for _, v in series], dtype=float)

    order = np.argsort(times)
    times = times[order]
    values = values[order]

    return np.interp(query_times, times, values)


def align_block(t0: float, n_samples: int, sample_rate_hz: float, series: Sequence[tuple]) -> np.ndarray:
    """Given a vibration block's start time/length/rate and a CAN series,
    return the series values linearly interpolated onto the block's
    per-sample time window."""
    query_times = block_sample_times(t0, n_samples, sample_rate_hz)
    return interpolate_series(series, query_times)
