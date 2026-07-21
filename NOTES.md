# NOTES: Dual-Band Vibration Processor Integration

Discovery + integration notes for the dual-band (trusted / extended)
vibration processor added in `src/processing/`. Read this before changing
`SAMPLE_RATE_HZ`, `fc`, or anything under `src/processing/`.

## 0. Repo discovery

This repo, as it stands, is a single flat script plus its predecessor
prototype -- **not** the package structure this feature's brief assumed
(`vibration_monitoring/processing/dual_band.py`, ~2 kHz block acquisition,
a CAN/align-step RPM source, an existing config module, an existing test
framework). Specifically, at the time this feature was built:

- **Acquisition**: `src/vibration_monitor.py` reads one scalar sample per
  axis at a time over SPI (`read_axis()`: send command, `time.sleep(0.001)`,
  read result), for x, y, z sequentially, paced to `SAMPLE_RATE_HZ = 100`
  by the main loop's `next_sample_time` bookkeeping. There is no discrete
  "block" object -- samples accumulate into three `deque(maxlen=WINDOW_SIZE)`
  buffers (`x_buf`/`y_buf`/`z_buf`, `WINDOW_SIZE = 256`), i.e. **1-D
  per-axis buffers**, not an N×3 block. The FFT window's timestamp is
  `time.time()` (wall clock) at the moment the 256th sample lands
  (`window_end_time` in the existing metrics CSV) -- there is no per-sample
  timestamp, only per-window.
- **RPM source**: none exists in this repo. No CAN reader, no tachometer
  input, no "align step." `git log` shows a `daq/` directory that *did*
  implement exactly this (2 kHz block acquisition via `acquire.py`, a CAN
  reader + `align.py` interpolation step, a `config.yaml`, and a pytest
  suite with a hardware-free SCA3300 simulator) — see "daq/ existed and
  was deleted" below. It was deleted from this repo the day before this
  feature was built. Per explicit instruction, this feature does **not**
  restore or depend on `daq/`; it integrates only with what remains
  (`src/vibration_monitor.py`). **TODO(RPM-SOURCE)**: wire a real RPM
  source into `vibration_monitor.py`'s `rpm = None` (see `main()`) before
  `extended_band_trend.csv`'s rising/baseline columns are meaningful.
- **Existing analysis stage**: `compute_axis_metrics()` /
  `compute_combined_metrics()` (per-axis and combined-magnitude FFT +
  RMS/peak/crest/kurtosis + up to 3 dominant-peak picking), and
  `HealthMonitor` (a 0-100 score combining a windowed RMS trend against
  the existing per-window results and an instantaneous per-sample spike
  check). Both are written to `data/metrics/vibration_metrics.csv` once
  per 256-sample window, and printed to stdout. There is no LTE/telemetry
  emit path in this repo -- CSV + stdout is the entire "emit" surface.
- **Config mechanism**: none as a separate module -- constants live at
  the top of `vibration_monitor.py` (`SAMPLE_RATE_HZ`, `WINDOW_SIZE`,
  calibration/health tuning). Added `src/config.py` for the new feature's
  config only (`DualBandConfig`, `TrendConfig`); the existing constants
  were left exactly where they are (touching them was unnecessary and
  riskier than adding alongside).
- **Test framework**: none existed. No `tests/` directory, no test
  runner in `requirements.txt`. Added `pytest` (see
  `requirements-dev.txt`, kept separate from the Pi's production
  `requirements.txt`) plus `pytest.ini` (`pythonpath = src`).
  `vibration_monitor.py` opens the real SPI bus at **module import time**
  (`spidev.SpiDev().open(0, 0)` runs as soon as the file is imported, with
  no guard) -- this is a pre-existing property, not something this change
  introduced, but it means the module cannot be imported on a machine
  without the `spidev` package and real hardware. `tests/conftest.py`
  stubs `spidev` in `sys.modules` before import so `test_regression.py`
  can exercise the module's pure functions.

### daq/ existed and was deleted

`git log --all` shows commits `f01517f`..`66e5e38` adding a `daq/`
directory: `sca3300.py` (SPI driver), `clock.py` (shared-clock/threaded
sampler), `acquire.py` (2 kHz, `block_size: 4096`, N×3 `Block.samples`),
`can_reader.py` + `align.py` (CAN RPM interpolated onto block sample
times), `config.yaml`, and a pytest suite (`daq/tests/`, including
`fakes.py`, a hardware-free SCA3300 protocol simulator). This is, near
verbatim, the acquisition/RPM/config/test infrastructure this build
brief's Step 0 describes. Commit `d6dc83b` ("cleanup", the day before this
feature was built) deleted all of it. Asked directly, the repo owner
confirmed: integrate using **only** what's currently in the repo (i.e.
`src/vibration_monitor.py`), not `daq/`. This is recorded here so the
mismatch between the brief's assumptions and the current tree isn't
mysterious later, and so `daq/`'s design (particularly `align.py`'s
linear-interpolation approach to pairing an RPM series with a block) is a
reasonable starting point *if and when* a real RPM source is wired in.

## 1. What was added

- `src/config.py` -- `DualBandConfig`, `TrendConfig` dataclasses (defaults
  per the build brief, all overridable).
- `src/processing/dual_band.py` -- `DualBandProcessor`: turns one
  axis's block (1-D array) into `TrustedBandResult` (0-70 Hz, no
  correction) + `ExtendedBandResult` (70-82 Hz, de-emphasis + noise-gated,
  always `uncalibrated=True`).
- `src/processing/trend.py` -- `ExtendedBandTrendTracker`: RPM-bucketed
  EMA baseline over the extended band's `level`, flags a relative rise.
- Wiring in `src/vibration_monitor.py` (additive -- see "Integration
  points" below).
- `tests/test_dual_band.py`, `tests/test_trend.py` -- the five required
  cases (recovery, noise-rejection, isolation, trend, regression regression is
  in `tests/test_regression.py`).

## 2. Integration points chosen

- **Where trusted output goes**: a new CSV, `data/metrics/dual_band_trusted.csv`,
  written once per window per axis, alongside (not merged into)
  `vibration_metrics.csv`. Chosen over adding columns to the existing
  `METRICS_HEADER`/`metrics_row()` so the existing metrics schema is
  byte-for-byte unchanged -- "additive" was read as "new file" rather than
  "new columns on an existing, possibly-already-consumed file."
- **Where extended output goes**: `data/metrics/extended_band_trend.csv`
  -- the "clearly separate, flagged channel" the brief asks for. Never
  passed to `HealthMonitor`, never referenced by `score`/`status`, and
  nothing in `main()`'s existing spike/window health-scoring block reads
  it. This is the entire enforcement of "extended output never reaches
  alarms/fault logic": there is no code path connecting
  `ExtendedBandResult` to `HealthMonitor` at all.
- **Per-block wiring**: added directly after the existing per-window
  metrics block in `main()`'s loop (same `if len(x_buf) == WINDOW_SIZE and
  sample_count % WINDOW_SIZE == 0:` gate, so it fires on the same
  256-sample cadence as the existing analysis) -- all existing lines
  before it are unmodified.
- **fs**: `DUAL_BAND_CONFIG = DualBandConfig(fs=float(SAMPLE_RATE_HZ))` in
  `vibration_monitor.py`, i.e. pinned to the loop's *actual* rate (100 Hz
  today), not `DualBandConfig`'s own 2000.0 default. See "Sample-rate gap"
  below for why 100 Hz makes the extended band a safe no-op today.

## 3. Sample-rate gap (read this before raising SAMPLE_RATE_HZ)

The build brief's extended band (70-82 Hz) and noise-gate band (95-180 Hz)
require `fs` > 2x the highest band edge (Nyquist) to be representable at
all -- in practice comfortably above ~400 Hz so the 95-180 Hz noise
estimate has real bins, not just the 1-2 nearest Nyquist rolls off to.
`vibration_monitor.py`'s existing loop runs at **100 Hz** (Nyquist 50 Hz).
At that rate, `ext_mask` and `noise_mask` are both empty for every block:
`DualBandProcessor._extended_result()` detects this
(`if not np.any(ext_mask)`) and returns `ExtendedBandResult(level=0.0,
reliable=False, snr=0.0)` -- a safe, always-unreliable no-op, not a crash
or a fabricated reading. `extended_band_trend.csv` will show
`reliable=0`/`level=0.0` on every row until this is addressed.

This was **not** silently fixed by raising `SAMPLE_RATE_HZ` here, for two
reasons:
1. It's a live-hardware timing change this environment cannot verify.
   `read_axis()` has two hardcoded `time.sleep(0.001)` settle delays per
   axis read (see `vibration_monitor.py`), so a full x/y/z sample has a
   hardcoded ≥3 ms floor (excluding SPI transfer time itself) -- a ceiling
   of roughly 300 Hz *before* accounting for Python/OS call overhead.
   Whether that's enough headroom above the ~400 Hz needed, and whether
   those sleeps can be safely shortened without violating the SCA3300's
   command-to-data-ready timing, needs confirming on real hardware.
2. Raising `SAMPLE_RATE_HZ` changes existing, working behavior for the
   *trusted*-side analysis too (FFT bin resolution, `WINDOW_SIZE`'s
   real-time duration, the health monitor's spike buffer, which is
   time- not sample-based via `SPIKE_BUFFER_SECONDS` so it should adapt,
   but this needs to be re-verified against real data, not assumed) --
   which the brief asked to keep additive/unbroken.

**TODO(SAMPLE-RATE)**: once real-hardware timing confirms it's safe, raise
`SAMPLE_RATE_HZ` (one constant, in `vibration_monitor.py`) to at least
~400-500 Hz and re-validate `read_axis()`'s achievable throughput at that
rate. No other code changes are needed -- `DUAL_BAND_CONFIG` already
tracks `SAMPLE_RATE_HZ` directly.

## 4. Two-path design and isolation guarantee

- **Trusted (0-70 Hz)**: `DualBandProcessor._trusted_result()` computes
  `band_rms`/sub-band RMS from `mag`/`freqs` masked to `f <= trusted_hi`
  only -- no gain, no correction, no reference to the extended band's
  code path at all. This is the entire isolation guarantee: nothing the
  extended path does (gain curve, noise gate, SNR, future changes to any
  of those) can reach the trusted numbers, because the trusted method
  never reads anything the extended method writes.
- **Extended (70-82 Hz)**: de-emphasis gain `G(f) = min(sqrt(1 +
  (f/fc)^2), gain_cap)` applied only to `ext_mask` bins, SNR-gated against
  a noise-band (95-180 Hz) median estimate. **Always** `uncalibrated=True`
  regardless of `reliable`.
- **Usage rule (enforced by wiring, not just convention)**: `main()`
  passes `db_result.extended` to `ExtendedBandTrendTracker.update()` and
  to `extended_band_trend.csv` only. It is never passed to `HealthMonitor`,
  never read by the `score`/`status` values that already drive
  `CRITICAL`/`WARNING` in the existing pipeline, and
  `ExtendedBandTrendTracker.update()` itself has no alarm/threshold output
  beyond `(rising, baseline)`, which is a relative-trend signal, not a
  pass/fail against an absolute limit. If a future change wants the
  extended band to influence an alarm, that is a deliberate, separate
  decision -- it does not happen by accident through this code path.

## 5. Values requiring confirmation before production

- `fc = 70.0` (`config.DualBandConfig.fc`) -- **CONFIRM against the
  SCA3300 datasheet** revision actually shipped with this sensor. Wrong
  fc silently miscalibrates the de-emphasis gain curve.
- `snr_threshold = 3.0`, `noise_lo`/`noise_hi = 95/180`, `rise_ratio =
  1.5`, `rpm_bucket_width = 50.0` -- all marked CONFIRM in `config.py`;
  none have been tuned against real baseline vibration data, only against
  synthetic test signals.
- `SAMPLE_RATE_HZ` -- see Section 3. Must be raised (with real-hardware
  timing verification) before the extended band produces anything but
  `reliable=False`/`level=0.0`.
- RPM source -- see Section 0's TODO(RPM-SOURCE). `rpm = None` in
  `main()` today; `extended_band_trend.csv` logs `rpm` as blank and skips
  the trend tracker entirely until this is wired in.
