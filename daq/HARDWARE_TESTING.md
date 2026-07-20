# Hardware testing

Everything in `tests/` (the default `python3 -m unittest discover -s tests`
suite) runs anywhere with no hardware -- it's what validated all the logic
in this build. But logic correctness isn't the same as "the real sensor and
CAN bus actually behave this way," and this build environment has no SPI
device and no CAN interface at all (not even a `vcan` virtual one -- no
`ip`/`modprobe` tooling available here either) to check that against.

`tests/hardware/` holds the tests that need real hardware to mean anything.
They're real `unittest.TestCase`s, not scripts, so they report pass/fail
the same way the rest of the suite does -- but every one of them **skips
itself** unless explicitly opted into with an environment variable, so
running the full suite anywhere (including this sandbox) stays safe by
default.

## Running

All commands below assume `cd daq` first, on the real Raspberry Pi with
the SCA3300 wired up and (for the CAN tests) the USB-CAN adapter attached
and brought up (`sudo ip link set can0 up type can bitrate 250000` --
`can_discover.py` will tell you the real bitrate/driver situation first).

```bash
# 1. SCA3300 link: WHOAMI/STATUS, CRC pass rate, gravity check, 2kHz timing.
#    Board must be held still for the gravity check.
DAQ_RUN_HARDWARE_TESTS=1 python3 -m unittest tests.hardware.test_sca3300_hardware -v

# 2. CAN adapter/bus: driver detection, live traffic, monotonic timestamps.
#    Needs actual bus traffic during the sniff window (engine running, or
#    a bench CAN simulator) -- a quiet bus fails this by design.
DAQ_RUN_HARDWARE_TESTS=1 python3 -m unittest tests.hardware.test_can_hardware -v

# 3. Manually confirm can_map.yaml's RPM/load/torque candidates against a
#    real spin-up or throttle change (see "Manual steps" below) -- no
#    test can do this one for you.
python3 tests/hardware/watch_rpm.py --duration 120

# 4. Full Task 2 acceptance test: sustained 2kHz through the real
#    Acquirer/SensorHub path, ~0 missed samples, p99 within +-5%.
#    Slow by design -- gated behind a second env var so it never fires
#    just because #1-2 were requested.
DAQ_RUN_HARDWARE_TESTS=1 DAQ_RUN_SOAK_TEST=1 python3 -m unittest tests.hardware.test_acquire_soak -v
```

Run them in that order -- each one builds confidence for the next. If (1)
doesn't hold 2kHz cleanly, (4) won't either, and if (2) can't find the
RPM/torque candidates, (3) has nothing to confirm.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DAQ_RUN_HARDWARE_TESTS` | unset (skip) | Master gate for all of `tests/hardware/`. |
| `DAQ_RUN_SOAK_TEST` | unset (skip) | Second gate, only for the slow 60s+ soak test. |
| `DAQ_CONFIG` | `config.yaml` | Config file to load, relative to `daq/`. |
| `DAQ_SENSOR_NAME` | first `type: sca3300` entry | Which `sensors:` entry `test_sca3300_hardware.py` probes. |
| `DAQ_SPI_BUS`, `DAQ_SPI_DEVICE`, `DAQ_SPI_SPEED`, `DAQ_SPI_MODE` | from config | Override that sensor's SPI settings without editing `config.yaml`. |
| `DAQ_TIMING_DURATION_S` | `10` | `test_sca3300_hardware.py`'s timing-characterization duration; set to `60` for the brief's full acceptance length. |
| `DAQ_CAN_IFACE`, `DAQ_CAN_BUSTYPE`, `DAQ_CAN_BITRATE` | from config | Override CAN settings without editing `config.yaml`. |
| `DAQ_CAN_SNIFF_DURATION_S` | `10` | `test_can_hardware.py`'s bus-sniff duration. |
| `DAQ_SOAK_DURATION_S` | `60` | `test_acquire_soak.py`'s run length -- the brief's own acceptance bar is "≥60s". |

## What each test actually certifies

- **`test_sca3300_hardware.py`** -- reuses `probe_sca3300.py`'s own
  functions (`gravity_check`, `crc_burst_check`, `timing_characterization`
  -- the same ones already unit-tested against a fake device in
  `tests/test_probe_sca3300.py`) but asserts pass/fail against the real
  chip instead of printing a report:
  - `test_whoami_and_status_clean` -- WHOAMI reads `0x51`, STATUS has no
    bits set and RS isn't an error, after the datasheet startup sequence.
  - `test_crc_pass_rate_is_effectively_100_percent` -- ≥99.9% of frames
    over a 2000-frame burst pass CRC, with zero RS errors. Below that,
    check wiring/SPI mode/clock speed before anything else.
  - `test_gravity_check_passes` -- exactly one axis reads ~±1g, the
    others ~0, confirming orientation and basic sanity of the readings
    (board must be held still).
  - `test_holds_2khz_cadence_within_acceptance_bar` -- the brief's own
    acceptance criterion: 0 missed intervals, p99 within ±5% of 500us,
    over `DAQ_TIMING_DURATION_S` seconds (default 10s for a quicker
    check; use 60 for the full-length one).

- **`test_can_hardware.py`**:
  - `test_adapter_detection_runs_and_reports_a_kind` -- confirms
    `detect_adapter()` returns a definite `native_socketcan`/`slcan`
    classification for the real adapter (or prints its raw evidence if
    it can't, so you can tell manually).
  - `test_bus_has_live_traffic_and_monotonic_timestamps` -- sniffs the
    real bus and asserts at least one ID was seen (a quiet bus is a
    failure here, not a pass) and that every ID's timestamps are
    monotonic.

- **`test_acquire_soak.py`** -- the actual Task 2 acceptance test end to
  end: runs every sensor in `config.yaml`'s `sensors:` list through the
  real `Acquirer` (SCHED_FIFO, CPU pinning, block assembly -- whatever
  `config.yaml` actually specifies) for `DAQ_SOAK_DURATION_S` seconds and
  asserts, per sensor: 0 missed samples, p99 within ±5% of that sensor's
  target period, 0 CRC/RS errors. This is a different (and more
  representative) code path than `test_sca3300_hardware.py`'s timing
  check, which exercises `sca3300.py` directly rather than the full
  acquisition pipeline.

## Manual steps -- not automatable, do these by hand

Some things genuinely need a human, not an assertion:

1. **Confirming `can_map.yaml`'s RPM/load/torque candidates.** Run
   `python3 tests/hardware/watch_rpm.py` while actually changing engine
   speed or throttle, and watch whether the printed values track reality.
   Only once you're confident should you set that signal's `confirmed:
   true` in `can_map.yaml`. `can_reader.py` refuses to use any signal
   where this isn't true (see `README.md`), so this step is a hard
   prerequisite for Task 2's CAN path to run at all, not just a
   recommendation.

2. **Confirming the still-unconfirmed datasheet values.** `README.md`'s
   "Datasheet assumptions" section lists exactly what couldn't be
   verified from this build environment: the TEMP register address
   (`0x05`), STATUS register bit-level semantics beyond "any bit set,"
   RS values `0b00`/`0b10`, and modes 2-4's g-range. None of these affect
   the Mode-1 vibration path this build actually uses, but if you change
   `spi.mode` away from 1 or start relying on `read_status()`'s
   individual bits or `read_temp_raw()`, pull the physical Murata
   SCA3300-D01 datasheet PDF (this environment's fetches to it were
   blocked -- a normal browser should have no trouble) and update
   `sca3300.py`'s register-map comments and `README.md` accordingly.

3. **Deciding on real-time configuration.** `isolcpus`/`SCHED_FIFO`
   privileges are a one-time system setup step (see `README.md`
   "Required privileges"), not something a test can grant on your
   behalf -- do that before running `test_acquire_soak.py`, or its
   result will just reflect whatever scheduling the Pi happened to give
   the process.

## If a hardware test fails

- **CRC pass rate low / gravity check fails**: check wiring first (SPI
  bus/CS in `config.yaml` matching the physical connection), then SPI
  mode and clock speed.
- **2kHz timing test fails (missed samples, p99 out of range)**: this is
  exactly the signal the brief calls out -- see `README.md`'s "MCU
  front-end fallback" section. Also check whether `realtime.cpu_core` is
  actually set to an `isolcpus`-isolated core and whether the process
  actually has `CAP_SYS_NICE` (`health_status()`'s `sched_fifo_active`
  field, printed by `test_acquire_soak.py`, tells you directly whether
  SCHED_FIFO was actually obtained rather than silently falling back).
- **CAN adapter kind is `unknown`**: not necessarily a failure --
  `detect_adapter()`'s heuristics (parsing `ip -details link show`,
  sysfs driver symlinks, `dmesg`) are best-effort. Cross-check the
  printed evidence manually; the actual driver/chipset docs for your
  specific USB-CAN adapter are the authority here.
- **No CAN traffic seen**: confirm the bus is actually live (engine
  running or bench simulator active) and the bitrate in `config.yaml`
  matches the real bus -- a bitrate mismatch typically looks exactly
  like "no traffic," not like framing errors.
