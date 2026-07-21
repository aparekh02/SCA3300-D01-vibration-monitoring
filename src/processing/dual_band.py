"""
dual_band.py

Turns one block of a single accelerometer axis into two isolated outputs:

  - trusted   (0 - trusted_hi, default 0-70 Hz): the SCA3300's fixed
    first-order low-pass corner (fc) leaves this range essentially flat,
    so it is reported with NO correction. Safe for the existing
    analysis/alarms.
  - extended  (ext_lo - ext_hi, default 70-82 Hz): the sensor's LPF skirt
    attenuates this range in a known way, so it is recoverable by
    inverting that response (de-emphasis) -- but only approximately, and
    only when the recovered signal clears a noise-floor SNR gate. Always
    flagged `uncalibrated`, and reliable/unreliable per-block via the SNR
    gate. NEVER trustworthy enough for alarms or fault logic -- see
    NOTES.md and the README's "Extended band usage rule".

Hard isolation requirement: `_trusted_result()` only ever reads
`mag`/`freqs` through `trusted_mask` (f <= trusted_hi) and applies no
gain/correction, so nothing computed for the extended band -- gain curve,
noise gate, SNR -- can influence the trusted output, regardless of how
those extended-band code paths change in the future.

Frequencies above ext_hi belong to no analysis band and are dropped
(present in the FFT but never read by either path): above ~82 Hz,
inverting the SCA3300's LPF response would mostly amplify noise rather
than recover signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np

from config import DualBandConfig, DEFAULT_DUAL_BAND_CONFIG

# (label, low_hz, high_hz) sub-bands reported within the trusted range,
# e.g. to separate shaft-order content (low) from early bearing tones
# (higher). Purely a reporting breakdown of the same no-correction trusted
# band -- not a separate calibration path.
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
    """Periodic (DFT-even) Hann window, length n.

    numpy.hanning(n) is the *symmetric* Hann window (zero at both
    endpoints), which is the wrong variant for spectral analysis -- it
    biases bin magnitudes vs. the periodic form assumed by the band_rms /
    SNR formulas below. The periodic form is np.hanning(n+1) with the
    last (duplicate-of-first) sample dropped; computed directly here since
    numpy has no built-in `sym=False` option (that's scipy.signal.hann).
    """
    if n <= 1:
        return np.ones(max(n, 0))
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)


def _band_rms(mag: np.ndarray, mask: np.ndarray, s1: float) -> float:
    if not np.any(mask) or s1 <= 0:
        return 0.0
    return float(np.sqrt(2.0 * np.sum(mag[mask] ** 2)) / s1)


class DualBandProcessor:
    """Stateless per-block processor; config is the only state, shared
    across axes/blocks (matches DualBandConfig being frozen/immutable)."""

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
        """No correction is applied anywhere in this method -- this is the
        entire isolation guarantee for the trusted path."""
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


def process_block(
    axes: Dict[str, np.ndarray], config: DualBandConfig = DEFAULT_DUAL_BAND_CONFIG
) -> Dict[str, DualBandResult]:
    """Convenience wrapper: run one DualBandProcessor over a dict of
    per-axis signals (e.g. {"x": x_buf, "y": y_buf, "z": z_buf}), matching
    vibration_monitor.py's existing per-axis buffer layout."""
    processor = DualBandProcessor(config)
    return {name: processor.process(sig) for name, sig in axes.items()}
