# Hardware testing

`tests/` (the default `python3 -m unittest discover -s tests` suite) runs
anywhere with no hardware -- it validated all the logic in this build, but
that isn't the same as "the real sensor and CAN bus actually behave this
way," and this build environment has no SPI device and no CAN interface
at all (not even `vcan` -- no `ip`/`modprobe` tooling here either).

`tests/hardware/` holds the tests that need real hardware to mean
anything. They're real `unittest.TestCase`s, but every one **skips
itself** unless explicitly opted into with an environment variable, so
running the full suite anywhere (including this sandbox) stays safe.

## Running

Assumes `cd daq` on the real Pi, SCA3300 wired up, USB-CAN adapter
attached and brought up (`can_discover.py` will tell you the real
bitrate/driver -- don't assume 250000).

```bash
# 1. SCA3300 link: WHOAMI/STATUS, CRC pass rate, gravity check, 2kHz timing.
#    Board must be held still for the gravity check.
DAQ_RUN_HARDWARE_TESTS=1 python3 -m unittest tests.hardware.test_sca3300_hardware -v

# 2. CAN adapter/bus: driver detection, live traffic, monotonic timestamps.
#    Needs actual bus traffic during the sniff window -- a quiet bus fails by design.
DAQ_RUN_HARDWARE_TESTS=1 python3 -m unittest tests.hardware.test_can_hardware -v

# 3. Manually confirm can_map.yaml's candidates against a real spin-up/
#    throttle change (see "Manual steps") -- no test can do this for you.
python3 tests/hardware/watch_rpm.py --duration 120

# 4. Full Task 2 acceptance test: sustained 2kHz, ~0 missed, p99 within +-5%.
#    Slow by design -- gated behind a second env var.
DAQ_RUN_HARDWARE_TESTS=1 DAQ_RUN_SOAK_TEST=1 python3 -m unittest tests.hardware.test_acquire_soak -v
```

Run in order -- each builds confidence for the next. If (1) doesn't hold
2kHz cleanly, (4) won't either; if (2) finds no candidates, (3) has
nothing to confirm.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DAQ_RUN_HARDWARE_TESTS` | unset (skip) | Master gate for all of `tests/hardware/`. |
| `DAQ_RUN_SOAK_TEST` | unset (skip) | Second gate, only for the slow soak test. |
| `DAQ_CONFIG` | `config.yaml` | Config file to load, relative to `daq/`. |
| `DAQ_SENSOR_NAME` | first `type: sca3300` entry | Which `sensors:` entry to probe. |
| `DAQ_SPI_BUS/DEVICE/SPEED/MODE` | from config | Override SPI settings without editing config.yaml. |
| `DAQ_TIMING_DURATION_S` | `10` | Timing-characterization duration (use `60` for the full acceptance length). |
| `DAQ_CAN_IFACE/BUSTYPE/BITRATE` | from config | Override CAN settings without editing config.yaml. |
| `DAQ_CAN_SNIFF_DURATION_S` | `10` | Bus-sniff duration. |
| `DAQ_SOAK_DURATION_S` | `60` | Soak run length -- the brief's own acceptance bar is "≥60s". |

## What each test certifies

- **`test_sca3300_hardware.py`** -- reuses `probe_sca3300.py`'s own
  functions (already unit-tested against a fake device) as pass/fail
  assertions against real hardware: WHOAMI/STATUS clean, CRC pass rate
  ≥99.9% with zero RS errors, gravity check (exactly one axis ~±1g), and
  the 2kHz cadence acceptance bar (0 missed, p99 within ±5%).
- **`test_can_hardware.py`** -- adapter detection reports a definite kind
  (or prints its evidence if it can't), and a bus sniff sees live traffic
  with monotonic timestamps (a quiet bus is a failure, not a pass).
- **`test_acquire_soak.py`** -- the actual Task 2 acceptance test end to
  end, through the real `Acquirer` (SCHED_FIFO, CPU pinning, block
  assembly), asserting 0 missed samples / p99 within ±5% / 0 CRC errors
  per sensor. A different (more representative) path than
  `test_sca3300_hardware.py`'s timing check, which exercises `sca3300.py`
  directly rather than the full pipeline.

## Manual steps -- not automatable

1. **Confirming `can_map.yaml`'s candidates.** Run `watch_rpm.py` while
   actually changing engine speed/throttle, and watch whether the printed
   values track reality. Only then set that signal's `confirmed: true` --
   `can_reader.py` refuses to use any signal where this isn't true, so
   this is a hard prerequisite for Task 2's CAN path, not a suggestion.
2. **Confirming the still-unconfirmed datasheet values.** README's
   "Datasheet assumptions" lists what couldn't be verified here (TEMP
   register, STATUS bit semantics, RS `0b00`/`0b10`, modes 2-4 g-range).
   None affect the Mode-1 path this build uses; if you change `spi.mode`
   or start relying on `read_status()`'s bits or `read_temp_raw()`, pull
   the real datasheet PDF (blocked from this environment, not from a
   normal browser) and update `sca3300.py`/README accordingly.
3. **Deciding real-time configuration.** `isolcpus`/`SCHED_FIFO` privileges
   are a one-time setup step (README "Required privileges"), not
   something a test can grant -- do it before `test_acquire_soak.py`, or
   its result just reflects whatever scheduling the Pi happened to give it.

## If a hardware test fails

- **CRC pass rate low / gravity check fails**: check wiring first (SPI
  bus/CS matching the physical connection), then SPI mode/clock speed.
- **2kHz timing fails**: see README's "MCU front-end fallback". Also
  check `realtime.cpu_core` is actually isolated and the process actually
  has `CAP_SYS_NICE` -- `health_status()`'s `sched_fifo_active` field
  (printed by `test_acquire_soak.py`) tells you directly.
- **CAN adapter kind is `unknown`**: not necessarily a failure --
  `detect_adapter()`'s heuristics are best-effort; cross-check the
  printed evidence against the adapter's actual chipset docs.
- **No CAN traffic seen**: confirm the bus is actually live and the
  bitrate matches -- a bitrate mismatch usually looks exactly like "no
  traffic," not framing errors.
