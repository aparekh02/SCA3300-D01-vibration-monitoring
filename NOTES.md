# NOTES: Dual-Band Vibration Processor Integration

## Repo discovery

This repo, before this change, was a single flat script (`src/vibration_monitor.py`)
with no package structure, config module, or test framework -- not what
the build brief assumed. Specifically:

- **Acquisition**: sequential per-axis SPI reads (`read_axis()`), paced
  to `SAMPLE_RATE_HZ` by the main loop. No discrete "block" object --
  samples accumulate into three 1-D `deque(maxlen=WINDOW_SIZE)` buffers
  (`x_buf`/`y_buf`/`z_buf`). Windows are timestamped by wall-clock time
  at the 512th sample, not per-sample.
- **RPM source**: none exists. `git log` shows a `daq/` directory that
  *did* implement 2 kHz block acquisition, a CAN reader, and
  `align.py` (block↔RPM interpolation) -- deleted in the commit right
  before this feature was built. Asked directly, confirmed: integrate
  using only what remains in the repo, not `daq/`. `rpm = None` in
  `main()` with a `TODO(RPM-SOURCE)` marker until a real source exists.
- **Existing analysis**: `compute_axis_metrics()`/`compute_combined_metrics()`
  (FFT + RMS/peak/crest/kurtosis + peak-picking) and `HealthMonitor`,
  written to `data/metrics/vibration_metrics.csv` once per window. No
  config module existed (constants live at the top of the file); added
  `src/config.py` for the new feature only. No test framework existed;
  added `pytest` (`requirements-dev.txt`, `pytest.ini`).
  `vibration_monitor.py` opens the real SPI bus at import time with no
  hardware fallback (pre-existing) -- `tests/conftest.py` stubs `spidev`
  so its pure functions can still be tested.

## Integration points

- Trusted output → new `data/metrics/dual_band_trusted.csv` (not merged
  into the existing `vibration_metrics.csv`, so that schema is unchanged).
- Extended output → new `data/metrics/extended_band_trend.csv`. Nothing
  in `main()` passes it to `HealthMonitor`/`score`/`status` -- that's the
  entire enforcement of "never reaches alarms/fault logic."
- Wired into the existing per-window block in `main()`'s loop (same
  `WINDOW_SIZE`-sample gate), after all existing lines, unmodified.
- `DUAL_BAND_CONFIG.fs` is pinned to `SAMPLE_RATE_HZ`, not
  `DualBandConfig`'s own 2000.0 default.

## Sample-rate

`SAMPLE_RATE_HZ` was 100 Hz (Nyquist 50 Hz) -- below even the extended
band, so neither the dual-band extended path nor the pre-existing
general FFT/peak-finder (no upper cutoff of its own) could see past
50 Hz. Raised to **200 Hz** (Nyquist 100 Hz, `WINDOW_SIZE` scaled
256→512 to keep the same window duration/bin width) at the user's
request, clearing the extended band (70-82 Hz) with margin. Chosen over
something higher because `read_axis()` has a hardcoded ~3ms settle-sleep
floor per x/y/z sample. Does **not** clear the 95-180 Hz noise-gate band
(needs fs > 360 Hz) -- the SNR estimate is correspondingly weaker.

Not verified on real hardware from here. Run `src/dual_band_hardware_check.py`
on the Pi to confirm the achieved rate/jitter, and back `SAMPLE_RATE_HZ`
off if it warns.

## Values requiring confirmation before production

- `fc = 70.0` (`config.DualBandConfig.fc`) -- confirm against the actual
  SCA3300 datasheet revision. Wrong `fc` miscalibrates the gain curve.
- `snr_threshold`, `noise_lo`/`noise_hi`, `rise_ratio`, `rpm_bucket_width`
  -- all marked CONFIRM in `config.py`, tuned only against synthetic
  test signals so far.
- RPM source -- see above.
