# Vibration Monitor FULL - SCA3300-D01

Engine-room vibration monitoring for a vessel, running on a Raspberry Pi
with a Murata SCA3300-D01 accelerometer over SPI.

## Layout

```
vibration_monitoring/
├── src/
│   └── vibration_monitor.py   # sensor read, FFT, calibration, health scoring
├── data/
│   ├── raw/                   # raw_vibration_log.csv (per-sample x/y/z)
│   └── metrics/                # vibration_metrics.csv (per-window FFT + health)
├── archive/
│   └── noise_reduct_acceler.py  # earlier prototype, kept for reference
├── requirements.txt
└── README.md
```

Generated CSVs in `data/` are git-ignored — only the folder structure
(`.gitkeep`) is tracked.

## Running

```
pip install -r requirements.txt
python3 src/vibration_monitor.py
```

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
