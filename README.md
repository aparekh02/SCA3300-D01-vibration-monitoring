# Vibration Monitor FULL - SCA3300-D01

Engine-room vibration monitoring for a vessel, running on a Raspberry Pi
with a Murata SCA3300-D01 accelerometer over SPI.

## Layout

```
vibration_monitoring/
├── src/
│   ├── vibration_monitor.py   # sensor read, FFT, calibration, health scoring
│   ├── dual_band_hardware_check.py  # run on the Pi: verify timing + show
│   │                                 # 70-82Hz+ content live (see below)
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

`vibration_monitor.py` opens the real SPI bus at import time with no
hardware fallback (predates this change); `tests/conftest.py` stubs
`spidev` so its pure functions can still be exercised — see `NOTES.md`.

`src/dual_band_hardware_check.py` is a separate, non-pytest script — run
it directly on the Pi with the sensor connected
(`python3 src/dual_band_hardware_check.py`) to check the actually-achieved
sample rate/jitter and see live 70 Hz+ content through both the general
FFT and the dual-band processor. See `NOTES.md`.

## What it does

- Reads X/Y/Z acceleration over SPI and logs every sample to
  `data/raw/raw_vibration_log.csv`.
- Calibrates a per-axis baseline at startup, then keeps adapting it —
  quickly while the axis is calm, and almost frozen while it's actively
  vibrating — so a resting axis (esp. Z) reads back to ~0 right after a
  vibration event ends, instead of decaying back over several seconds.
- Every 512-sample window (~2.56s at 200 Hz), runs an FFT per axis and
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
inverting it would mostly amplify noise. `src/processing/dual_band.py`
splits each window into two **isolated** outputs per axis, additive
alongside the existing analysis above:

- **Trusted (0-70 Hz)** — no correction, `validated=True`. Written to
  `data/metrics/dual_band_trusted.csv` (a new file, not merged into
  `vibration_metrics.csv`).
- **Extended (70-82 Hz)** — de-emphasis gain + noise-floor SNR gate.
  **Always** `uncalibrated=True`, usable only as a relative trend
  (`src/processing/trend.py`'s RPM-bucketed EMA tracker). Written to
  `data/metrics/extended_band_trend.csv`.

**Isolation**: the trusted result reads only bins ≤ 70 Hz with no
correction — nothing the extended path computes can affect it. See
`DualBandProcessor._trusted_result()`.

**Usage rule**: extended output must **never** feed an alarm or
`HealthMonitor`/fault logic. Enforced by wiring — `main()` only ever
passes it to the trend tracker / `extended_band_trend.csv`.

**Before production**: `fc`, `snr_threshold`, the noise band, and the
trend tracker's `rise_ratio`/`rpm_bucket_width` are all placeholder
defaults (marked CONFIRM in `config.py`), tuned only against synthetic
signals so far. See `NOTES.md` for the sample-rate/RPM-source gaps.
