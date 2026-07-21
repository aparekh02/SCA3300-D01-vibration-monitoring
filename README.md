# Vibration Monitor FULL - SCA3300-D01

Engine-room vibration monitoring for a vessel, running on a Raspberry Pi
with a Murata SCA3300-D01 accelerometer over SPI.

## Layout

```
vibration_monitoring/
├── src/
│   ├── vibration_monitor.py   # sensor read, FFT, calibration, health scoring
│   ├── config.py               # dual-band processor / trend tracker config
│   └── processing/
│       ├── dual_band.py         # DualBandProcessor (trusted + extended bands)
│       └── trend.py              # ExtendedBandTrendTracker (RPM-bucketed EMA)
├── tests/                      # pytest suite (dual-band + trend + regression)
├── data/
│   ├── raw/                   # raw_vibration_log.csv (per-sample x/y/z)
│   └── metrics/                # vibration_metrics.csv (per-window FFT + health)
│                                # + dual_band_trusted.csv / extended_band_trend.csv
├── archive/
│   └── noise_reduct_acceler.py  # earlier prototype, kept for reference
├── requirements.txt
├── requirements-dev.txt        # requirements.txt + pytest
├── pytest.ini
├── NOTES.md                    # dual-band integration notes (read before tuning)
└── README.md
```

Generated CSVs in `data/` are git-ignored — only the folder structure
(`.gitkeep`) is tracked.

## Running

```
pip install -r requirements.txt
python3 src/vibration_monitor.py
```

## Testing

```
pip install -r requirements-dev.txt
pytest
```

Only `src/processing/` and `src/config.py` are meaningfully unit-tested
(pure numpy, no hardware). `vibration_monitor.py` opens the real SPI bus
at import time with no hardware fallback, which predates this change;
`tests/conftest.py` stubs `spidev` so its pure functions (FFT, kurtosis,
health scoring) can still be exercised — see `NOTES.md` Section 0.

## What it does

- Reads X/Y/Z acceleration over SPI and logs every sample to
  `data/raw/raw_vibration_log.csv`.
- Calibrates a per-axis baseline at startup, then keeps adapting it —
  quickly while the axis is calm, and almost frozen while it's actively
  vibrating — so a resting axis (esp. Z) reads back to ~0 right after a
  vibration event ends, instead of decaying back over several seconds.
- Every 256-sample window (~2.56s at 100 Hz), runs an FFT per axis and
  on the combined X/Y/Z vibration magnitude. The combined spectrum is
  broken into up to 3 distinct peaks, so multiple simultaneous
  vibration sources (e.g. shaft rotation vs. a bearing/pump tone) show
  up separately instead of collapsing into one "dominant frequency".
- Scores overall health 0-100 via a lightweight statistical model (no
  training data needed):
  - a slow-moving "normal" RMS baseline that sustained elevated
    vibration erodes the score against,
  - a per-sample instant-spike check (magnitude vs. the recent
    short-term noise floor) that drops the score immediately on a
    sudden, large event rather than waiting for a window to fill.
  Status buckets: `OK` / `WARNING` / `ABNORMAL - inspect` /
  `CRITICAL - fix needed`.
- All of the above is logged per window to
  `data/metrics/vibration_metrics.csv`.

## Dual-band vibration processor

The SCA3300 has a fixed first-order low-pass at ~70 Hz (Mode 1). Below
that, its response is flat and trustworthy; from 70-82 Hz it's
attenuated but recoverable by inverting the known response; above ~82 Hz,
inverting that response would mostly amplify noise. `src/processing/dual_band.py`
splits each window into two **isolated** outputs per axis, run
additively alongside the existing per-axis/combined analysis above:

- **Trusted (0-70 Hz)** — no correction applied. `validated=True`.
  Written to `data/metrics/dual_band_trusted.csv` (broadband RMS + 0-10 /
  10-30 / 30-70 Hz sub-bands), a new file alongside (not merged into)
  `vibration_metrics.csv`.
- **Extended (70-82 Hz)** — de-emphasis gain + a noise-floor SNR gate
  (see `config.DualBandConfig`). **Always** flagged `uncalibrated=True`,
  and only usable as a *relative trend*, never an absolute reading.
  `src/processing/trend.py`'s `ExtendedBandTrendTracker` tracks an
  RPM-bucketed EMA baseline of its `level` and flags a rise. Written to
  `data/metrics/extended_band_trend.csv`.

**Isolation guarantee**: the trusted result is computed only from FFT
bins ≤ 70 Hz with no correction — the code path that produces it never
reads anything the extended-band code path writes (gain curve, noise
gate, SNR), so nothing in the extended path can alter or degrade it. See
`DualBandProcessor._trusted_result()`.

**Extended-band usage rule**: the extended output must **never** feed an
alarm threshold or the existing `HealthMonitor`/fault logic. This is
enforced by wiring, not just convention — `main()` only ever passes the
extended result to the trend tracker and to `extended_band_trend.csv`;
there is no code path connecting it to `score`/`status`/`HealthMonitor`.
If a future change wants extended-band data to influence an alarm, that
must be a new, deliberate decision, not a side effect of this one.

**Before production**: `fc` (`config.DualBandConfig.fc`, default 70.0)
must be confirmed against the SCA3300 datasheet revision actually shipped
with this sensor. `snr_threshold`, the noise band (`noise_lo`/`noise_hi`),
and the trend tracker's `rise_ratio`/`rpm_bucket_width` are all
placeholder defaults (marked CONFIRM in `config.py`) and must be tuned
against real baseline vibration data, not synthetic test signals.

**Known gap**: the live acquisition loop runs at `SAMPLE_RATE_HZ = 100`
(Nyquist 50 Hz), below both the extended band (70-82 Hz) and its
noise-gate band (95-180 Hz) — so today, every block's extended result is
a safe no-op (`reliable=False`, `level=0.0`), not a crash or a fabricated
reading. There is also currently no RPM source in this repo to pair with
a block, so `extended_band_trend.csv`'s `rpm` column is blank and the
trend tracker is not invoked (`main()`'s `rpm = None`). See `NOTES.md`
for what's needed to close both gaps and why they weren't done silently
here.
