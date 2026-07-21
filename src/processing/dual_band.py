"""Turns one accelerometer-axis block into two isolated outputs:

  - trusted  (0-70 Hz default): below the SCA3300's fixed LPF corner
    (fc), reported with NO correction. Safe for existing analysis/alarms.
  - extended (70-82 Hz default): the LPF skirt, recovered by inverting
    the known response (de-emphasis) and SNR-gated against a noise band.
    Always `uncalibrated=True`. NEVER trustworthy enough for alarms or
    fault logic -- see NOTES.md / README "Extended band usage rule".

Isolation: `_trusted_result()` only reads bins through `trusted_mask`
(f <= trusted_hi) with no correction, so nothing the extended path
computes (gain, noise gate, SNR) can reach it. Frequencies above ext_hi
belong to no band and are dropped -- inverting the LPF response there
would mostly amplify noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np

from config import DualBandConfig, DEFAULT_DUAL_BAND_CONFIG

TRUSTED_SUB_BANDS = (
    ("0_10hz", 0.0, 10.0),
    ("10_30hz", 10.0, 30.0),
    ("30_70hz", 30.0, 70.0),
)


@dataclass(frozen=True)
class TrustedBandResult:
    validated: bool
    broadband_rms: float
    sub_band_rms: Dict[str, float]


@dataclass(frozen=True)
class ExtendedBandResult:
    level: float
    reliable: bool
    snr: float
    uncalibrated: bool = True


@dataclass(frozen=True)
class DualBandResult:
    trusted: TrustedBandResult
    extended: ExtendedBandResult


def _periodic_hann(n: int) -> np.ndarray:
    """Periodic (DFT-even) Hann window -- numpy.hanning(n) is the
    symmetric variant (zero at both endpoints), the wrong one for
    spectral analysis; numpy has no built-in periodic option."""
    if n <= 1:
        return np.ones(max(n, 0))
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)


def _band_rms(mag: np.ndarray, mask: np.ndarray, s1: float) -> float:
    if not np.any(mask) or s1 <= 0:
        return 0.0
    return float(np.sqrt(2.0 * np.sum(mag[mask] ** 2)) / s1)


class DualBandProcessor:
    """Stateless per-block processor."""

    def __init__(self, config: DualBandConfig = DEFAULT_DUAL_BAND_CONFIG):
        self.config = config

    def process(self, signal: np.ndarray) -> DualBandResult:
        cfg = self.config
        signal = np.asarray(signal, dtype=float)
        n = len(signal)

        if cfg.detrend:
            signal = signal - np.mean(signal)

        w = _periodic_hann(n)
        s1 = float(np.sum(w))
        spectrum = np.fft.rfft(signal * w)
        mag = np.abs(spectrum)
        freqs = np.fft.rfftfreq(n, d=1.0 / cfg.fs)

        trusted_mask = freqs <= cfg.trusted_hi
        ext_mask = (freqs > cfg.ext_lo) & (freqs <= cfg.ext_hi)
        noise_mask = (freqs >= cfg.noise_lo) & (freqs <= cfg.noise_hi)

        trusted = self._trusted_result(mag, freqs, trusted_mask, s1)
        extended = self._extended_result(mag, freqs, ext_mask, noise_mask, s1)
        return DualBandResult(trusted=trusted, extended=extended)

    @staticmethod
    def _trusted_result(
        mag: np.ndarray, freqs: np.ndarray, trusted_mask: np.ndarray, s1: float
    ) -> TrustedBandResult:
        """No correction applied anywhere here -- the isolation guarantee."""
        broadband = _band_rms(mag, trusted_mask, s1)
        sub_bands = {}
        for name, lo, hi in TRUSTED_SUB_BANDS:
            sub_mask = trusted_mask & (freqs >= lo) & (freqs <= hi)
            sub_bands[name] = _band_rms(mag, sub_mask, s1)
        return TrustedBandResult(
            validated=True, broadband_rms=broadband, sub_band_rms=sub_bands
        )

    def _extended_result(
        self,
        mag: np.ndarray,
        freqs: np.ndarray,
        ext_mask: np.ndarray,
        noise_mask: np.ndarray,
        s1: float,
    ) -> ExtendedBandResult:
        cfg = self.config
        if not np.any(ext_mask) or s1 <= 0:
            # fs too low to represent ext_lo-ext_hi at all (see NOTES.md) --
            # a safe, explicitly-unreliable no-op rather than a crash.
            return ExtendedBandResult(level=0.0, reliable=False, snr=0.0)

        amp = np.sqrt(2.0) * mag / s1
        noise_amp = float(np.median(amp[noise_mask])) if np.any(noise_mask) else 0.0

        f_ext = freqs[ext_mask]
        gain = np.minimum(np.sqrt(1.0 + (f_ext / cfg.fc) ** 2), cfg.gain_cap)

        corrected = gain * amp[ext_mask]
        ext_energy = float(np.sum(corrected**2))
        noise_energy = float(np.sum((gain * noise_amp) ** 2))

        if noise_energy > 0:
            snr = ext_energy / noise_energy
        else:
            snr = float("inf") if ext_energy > 0 else 0.0

        return ExtendedBandResult(
            level=float(np.sqrt(ext_energy)),
            reliable=snr >= cfg.snr_threshold,
            snr=snr,
        )
