# daq/ - Sensor Acquisition & Discovery Layer

A hardware-facing acquisition layer meant to run as a standing service on
the Pi, that other software (analytics, dashboards, alerting) builds on
top of. Two things live here, in build order:

1. **Verification/discovery tools** (`probe_sca3300.py`, `can_discover.py`) --
   run these first, on the real Pi/hardware, before trusting anything else.
2. **A deterministic acquisition path** (`sca3300.py`, `acquire.py`,
   `can_reader.py`, `align.py`) that produces evenly-sampled vibration
   blocks and a time-aligned RPM/load/torque series, built on a **shared
   clock** (`clock.py`) so more sensors can be added later, each running
   concurrently on its own thread but sharing one timebase -- see
   `CLOCKING.md`.

**No FFT, order tracking, or health/diagnostics scoring is implemented
here** -- that's a separate, later task. This folder only acquires and
aligns data, and doesn't touch or depend on `src/`, `data/`, or `archive/`
at the repo root (a separate, already-working 100Hz prototype).

---

## Layout

```
daq/
├── sca3300.py            # SCA3300 SPI driver: startup, CRC/RS validation, reads
├── clock.py               # SharedClock/Ticker/RealTimeSampler/SensorHub -- the
│                          # multi-sensor-capable clocking mechanism (see CLOCKING.md)
├── probe_sca3300.py       # Task 1a: SCA3300 verification/discovery CLI
├── can_discover.py        # Task 1b: CAN adapter/bus discovery CLI
├── can_reader.py          # Task 2: background CAN signal reader
├── acquire.py             # Task 2: entry point (2kHz sampler + block assembly)
├── align.py               # Task 2: block <-> CAN-series linear interpolation
├── j1939.py               # shared J1939 ID/PGN/SPN decode helpers
├── config.yaml            # all hardware-specific values (SPI, rates, CAN, etc.)
├── can_map.todo.yaml       # template the human fills in from can_discover.py output
├── requirements.txt
├── pyproject.toml          # `pip install -e .` packaging -- see "Integration contract"
├── CLOCKING.md             # multi-sensor clocking design + worked example
├── HARDWARE_TESTING.md     # how to run tests/hardware/ on real hardware
├── deploy/                 # production install: systemd units, udev rule, install script
│   ├── install.sh            # run this on the Pi -- see "Deploying to a Raspberry Pi"
│   ├── daq-acquire.service
│   ├── daq-can0-up.service
│   └── 99-daq-hardware.rules
├── tests/
│   ├── fakes.py             # SCA3300 protocol simulator (no hardware needed)
│   ├── test_*.py            # one file per module, hardware-free (see "Tests")
│   └── hardware/             # real-hardware-only tests, self-skip without opt-in env var
│       ├── test_sca3300_hardware.py / test_can_hardware.py / test_acquire_soak.py
│       └── watch_rpm.py       # manual can_map.yaml confirmation helper (not a test)
└── README.md               # this file
```

---

## Setup (local dev iteration, e.g. SSH'd into the Pi directly)

```bash
cd daq
pip install -r requirements.txt
```

Requires Python 3.11+ on Raspberry Pi OS with:
- SPI enabled (`raspi-config` -> Interface Options -> SPI), sensor wired per
  `config.yaml`'s `sensors[].spi.bus` / `sensors[].spi.device`.
- A USB-CAN adapter enumerated as a SocketCAN interface (`ip link show`
  should list it, e.g. `can0`), brought up at the correct bitrate:
  ```bash
  sudo ip link set can0 up type can bitrate 250000
  ```
  (`can_discover.py` will tell you the actual driver/bitrate -- don't
  assume 250000 without confirming.)

For a production install other software will run against, see "Deploying
to a Raspberry Pi" below instead.

### Required privileges

- **SPI**: needs read/write access to `/dev/spidev*` (member of the `spi`
  group on most Raspberry Pi OS images, or root).
- **Real-time scheduling**: `SCHED_FIFO` requires root or `CAP_SYS_NICE`.
  Without it, the default (`sensors[].realtime.required: false`) logs a
  warning and runs at normal scheduling. Every sampler's `health_status()`
  reports `sched_fifo_active`/`cpu_pinned` regardless, and
  `realtime.required: true` turns a denial into a hard startup failure
  instead of a log line easy to miss. Grant the capability instead of
  running as root where possible:
  ```bash
  sudo setcap cap_sys_nice+ep $(readlink -f $(which python3))
  ```
- **CPU isolation** (recommended): isolate a core from the general
  scheduler by adding to `/boot/cmdline.txt` (or `/boot/firmware/cmdline.txt`):
  ```
  isolcpus=3 nohz_full=3 rcu_nocbs=3
  ```
  then reboot and set that sensor's `realtime.cpu_core: 3` in
  `config.yaml` (leave core 0 for the OS). Give each concurrently-running
  sensor its own isolated core if you can -- see CLOCKING.md "GIL and
  concurrent high-rate sensors" for why.
- **CAN**: bringing an interface up needs `CAP_NET_ADMIN` (via `sudo`);
  reading/writing frames once it's up does not.

### Running

```bash
# Task 1 -- run these first, on real hardware
python3 probe_sca3300.py --duration 60
python3 can_discover.py --duration 60

# review can_map.todo.yaml, confirm entries against a real spin-up/
# throttle change, then save the confirmed ones as can_map.yaml

# Task 2
python3 acquire.py                 # runs until Ctrl+C
```

---

## Deploying to a Raspberry Pi (production install)

Treats this like firmware: a fixed install location, a systemd service
that starts at boot and restarts on crash, boot-time CAN bring-up, and a
stable surface other software can depend on. `daq/deploy/` has everything
this section installs. Target: Raspberry Pi OS (Bookworm+), Python 3.11+.

### 1. Prepare the Pi (one-time)

```bash
sudo raspi-config   # Interface Options -> SPI -> enable, then reboot
```

Wire the SCA3300 and plug in the USB-CAN adapter, then confirm both are
visible:

```bash
ls /dev/spidev*                 # should list e.g. /dev/spidev0.0
ip link show                    # should list a CAN interface, e.g. can0
```

If no CAN interface shows up, check `dmesg | tail` for driver load
messages -- that's a wiring/driver problem, not config.

### 2. Upload the code

Deploys **only `daq/`** to a fixed location, `/opt/daq` -- the path the
systemd units in `daq/deploy/` assume.

```bash
rsync -avz --delete \
    --exclude '__pycache__' --exclude 'data/raw/*.npz' --exclude 'venv' \
    ./daq/ pi@<pi-host>:/opt/daq/
```

Or, if your team tracks this repo's git history on the Pi directly:

```bash
ssh pi@<pi-host> "git clone --branch <branch> <repo-url> /opt/vibration-monitoring && \
                   sudo ln -s /opt/vibration-monitoring/daq /opt/daq"
```

### 3. Install as a system service

```bash
ssh pi@<pi-host>
cd /opt/daq/deploy
sudo ./install.sh
```

Idempotent (safe to re-run after an update). Creates an unprivileged `daq`
system user, a venv at `/opt/daq/venv` with this package `pip install -e`d
into it, installs `99-daq-hardware.rules` (SPI permissions), and installs
+ enables `daq-can0-up.service` (brings `can0` up at boot -- **keep its
hardcoded bitrate in sync with `config.yaml`'s `can.bitrate` by hand**)
and `daq-acquire.service` (runs `acquire.py` as the `daq` user, auto-
restarts, grants `CAP_SYS_NICE` via `AmbientCapabilities`).

```bash
systemctl status daq-acquire.service     # confirm it's running
journalctl -u daq-acquire.service -f     # tail its logs
```

### 4. Verify before relying on it

```bash
sudo systemctl stop daq-acquire.service   # free the SPI/CAN devices first
cd /opt/daq && source venv/bin/activate
python3 probe_sca3300.py --duration 60
python3 can_discover.py --duration 60
# review can_map.todo.yaml, confirm against a real spin-up, save as can_map.yaml
DAQ_RUN_HARDWARE_TESTS=1 python3 -m unittest tests.hardware.test_sca3300_hardware tests.hardware.test_can_hardware -v
sudo systemctl start daq-acquire.service
```

See `HARDWARE_TESTING.md` for the full suite, including the soak test
that certifies Task 2's actual acceptance criterion (`DAQ_RUN_SOAK_TEST=1`).

### 5. Updating / uninstalling

```bash
# update
rsync -avz --delete --exclude '__pycache__' --exclude 'data/raw/*.npz' --exclude 'venv' \
    ./daq/ pi@<pi-host>:/opt/daq/
ssh pi@<pi-host> "cd /opt/daq/deploy && sudo ./install.sh && sudo systemctl restart daq-acquire.service"

# uninstall
sudo systemctl disable --now daq-acquire.service daq-can0-up.service
sudo rm /etc/systemd/system/daq-{acquire,can0-up}.service /etc/udev/rules.d/99-daq-hardware.rules
sudo systemctl daemon-reload
sudo userdel daq   # only if nothing else needs that account
```

(`--exclude venv` matters for updates: `venv/` only exists on the Pi, and
`--delete` would otherwise wipe it since it isn't in your local checkout.)

---

## Integration contract for other software

What another team's software on the same Pi can depend on today, and
what it can't yet:

- **Python import surface**: after `pip install -e /opt/daq` (already
  done by `install.sh`), `sca3300`, `clock`, `align`, `can_reader`,
  `j1939`, and `acquire` (for `Acquirer`) are importable from anywhere --
  the same set `pyproject.toml` declares. `probe_sca3300.py`/
  `can_discover.py` are verification CLIs, not part of this surface.
- **On-disk vibration blocks**: with `logging.write_blocks_to_disk: true`,
  each sensor writes `<raw_dir>/<sensor_name>_<t0_ns>.npz` containing
  `samples` ((n,3) g array), `t0_ns`, `sample_rate_hz`, `missed_in_block`.
  No push/pub-sub -- a consumer polls or watches `raw_dir` itself.
- **In-process only today**: CAN RPM/load/torque
  (`CanReader.series_snapshot()`) and live health (`Acquirer.health_status()`)
  are only available to code in the *same process* as `acquire.py` -- no
  file/socket export yet. A separate consumer only gets raw vibration
  blocks, not aligned RPM alongside them.
- **`align.py` is the seam, not a finished pipeline**: built and unit
  tested, but nothing in `acquire.py` calls it automatically -- consumers
  call `align_block()` themselves (see `CLOCKING.md`'s worked example).
- **Versioning**: `pyproject.toml` pins `0.1.0`, no changelog yet -- bump
  on breaking changes once other software actually depends on this.

### Tests

```bash
cd daq
python3 -m unittest discover -s tests
```

Always safe to run anywhere, including this build environment -- it
includes `tests/hardware/`, but those self-skip unless opted into (see
`HARDWARE_TESTING.md`).

`tests/fakes.py` simulates the SCA3300 protocol (independent CRC-8
implementation, so a bug in `sca3300.py`'s own CRC can't pass a test that
checks itself), including precise CRC-error injection. CAN tests use
synthetic `can.Message`s and hand-built J1939 IDs (no `vcan` available in
this sandbox to test against a real virtual bus).

- `test_sca3300.py` -- CRC/frame construction, startup success/failure,
  gravity-accurate reads, CRC-error detection and reinit recovery.
- `test_j1939.py` -- 29-bit ID -> PGN/SA decomposition, SPN extraction.
- `test_can_reader.py` -- `can_map.yaml` loading, message matching,
  wall-clock-to-monotonic timestamp offset.
- `test_can_discover.py` -- adapter detection, bus analysis, and that the
  generated `can_map.todo.yaml` schema matches what `can_reader.py` needs.
- `test_probe_sca3300.py` -- gravity/CRC-burst/timing-characterization logic.
- `test_clock.py` -- deadline-grid alignment, missed-deadline resync,
  block/health tracking, two sensors staying independent,
  `realtime.required` raising (with rollback), and a measured (not
  asserted-away) GIL-contention characterization -- see CLOCKING.md.
- `test_acquire.py` -- the SCA3300 adapter, a full `Acquirer` cycle, and a
  config-driven two-sensor scenario (no extra code, just `sensors:` entries).
- `test_align.py` -- interpolation correctness, range clamping, block alignment.

---

## Datasheet assumptions -- what's confirmed vs. still needs a check

This build environment couldn't reach the primary Murata SCA3300-D01 PDF
(Mouser/Murata/LCSC hosts all 403'd). Instead, every SPI-protocol constant
was **cross-verified against three independent sources that all agree**:

1. This repo's own already-tested driver, `src/vibration_monitor.py`,
   whose frame bytes produced the real output in `example_run.md`.
2. Murata's Linux IIO kernel driver (`drivers/iio/accel/sca3300.c`).
3. The `algebratech/sca3300-driver` Python reference implementation.

`sca3300.py` builds each frame from a register address + CRC computed
live (doesn't hardcode hex constants), verified programmatically to match
all three sources' literal bytes exactly (SW_RESET, mode-1 select, STATUS,
ACC_X/Y/Z, WHOAMI).

### Confirmed (byte-exact match across all 3 sources)

- SPI mode 0, 32-bit frames, MSB-first.
- CRC-8: poly `0x1D`, init `0xFF`, over the first 3 bytes, transmitted
  value is the bitwise NOT of the raw result.
- Frame byte 0 = `(write << 7) | (register_address << 2)` for requests;
  response byte 0's low 2 bits are RS (return status).
- Registers: `ACC_X=0x01`, `ACC_Y=0x02`, `ACC_Z=0x03`, `STATUS=0x06`,
  `MODE=0x0D`, `WHOAMI=0x10` (expected `0x51`).
- Software reset = write `0x0020` to MODE; mode select = write
  `0x0000`-`0x0003` to MODE for modes 1-4.
- **Mode 1**: 2700 LSB/g, 70Hz first-order LPF (matches the brief's
  "Default Mode 1 = +/-3g, 70Hz LPF"). Only mode `acquire.py`/
  `probe_sca3300.py` are designed and tested against.
- RS `0b11` = error (all sources agree); `0b01` is this repo's own
  driver's observed post-startup value (`example_run.md`).

### NOT independently confirmed -- verify before relying on them

- **TEMP register (`0x05`)**: inferred from register-map ordering, not
  the primary datasheet. `read_temp_raw()` returns the raw frame only --
  no raw-to-Celsius formula, since that constant couldn't be confirmed.
- **STATUS bit-level semantics**: `read_status()` reports raw value, RS,
  and a bit array, but individual bit *meanings* beyond "any bit set =
  not clean" aren't implemented -- cross-reference the real datasheet's
  Status Summary table before relying on them.
- **RS `0b00`/`0b10`**: only `0b11` (error) and `0b01` (post-startup) are
  used in logic; the other two are treated as "not an error" but their
  precise meaning isn't asserted.
- **Modes 2-4 g-range**: sensitivity/LPF cross-referenced, but g-range
  isn't (only Mode 1's +/-3g is). Irrelevant unless `spi.mode` != 1.
- **Exact minimum inter-frame idle time**: the Linux driver applies a 10us
  SPI delay; `sca3300.py` doesn't add one explicitly (two separate
  `xfer2()` calls already toggle CS). If CRC pass rate is below ~100%,
  check this first.
- **Startup settle timings**: the brief says "~15ms" after mode select;
  this repo's tested driver used 5ms/20ms and worked. Both are
  parameters (`start_up(post_reset_delay_s=..., post_mode_delay_s=...)`)
  -- tune against real hardware if needed.
- **J1939 SPN byte layouts** (`j1939.py: KNOWN_SIGNALS`): EEC1 SPN190
  (Engine Speed), SPN513 (Actual Engine %Torque), EEC2 SPN92 (% Load)
  follow the commonly published SAE J1939-71 layout, not a guarantee for
  any specific vessel's ECU -- exactly why `can_discover.py` surfaces
  candidates instead of trusting them, and `can_map.todo.yaml` requires
  `confirmed: true` (human-checked against a real spin-up) before
  `can_reader.py` will use a signal.

---

## Task 1 findings

`probe_sca3300.py`/`can_discover.py` were smoke-tested for logic
correctness (CRC/frame construction byte-verified, J1939 decode against
synthetic vectors, graceful-failure paths exercised) but **neither has
been run against real hardware** -- this build environment has no SPI
device or CAN adapter. `tests/hardware/` (see `HARDWARE_TESTING.md`) turns
that validation into real, gated, runnable tests. Run both tools on the
real Pi and keep their `*_result.json` for the record before trusting
anything downstream.

What to look for: `probe_sca3300.py`'s CRC pass rate should be ~100%, the
gravity check should show exactly one axis near +/-1g, and its `p99`/
`missed_count` decide whether Task 2's 2kHz target is achievable in pure
Python on this Pi (see fallback below). `can_discover.py`'s adapter `kind`
should match the adapter's actual chipset, and at least one EEC1/EEC2
candidate should show up if the bus is J1939 -- if not, the bus is likely
proprietary; fill `can_map.todo.yaml` in manually from the observed IDs.

---

## MCU front-end fallback (not built -- document only)

If `probe_sca3300.py` shows the Pi can't hold 2kHz within +/-5% jitter,
**do not force it**. The recommended path is a small microcontroller
front-end (e.g. RP2040/STM32) that samples the SCA3300 on a hardware
timer at a true 2kHz, buffers evenly-sampled blocks matching this repo's
`block_size` convention, and streams them to the Pi over USB-serial,
each tagged with an MCU-side monotonic timestamp for the first sample.
The Pi side would need a thin replacement for `acquire.py`'s sampling
loop reading framed blocks from serial instead of driving SPI --
`align.py`, `can_reader.py`, and the block/health model are unaffected,
since they only depend on receiving `(t0, samples[N,3])` blocks.

This is a TODO, not implemented -- `acquire.py` always drives SPI
directly today.

---

## Design notes / TODOs

- **SPI read pattern**: `sca3300.py` reads each axis with a
  request-then-NOP pair (2 transfers/register) rather than a rolling
  pipeline -- matches the tested pattern in `src/vibration_monitor.py`
  and leaves margin inside the 500us/sample budget. A leaner 1-transfer
  pipeline is possible if timing data ever shows Python/spidev call
  overhead (not raw transfer time) is the bottleneck.
- **CAN timestamp alignment**: python-can's SocketCAN timestamps are
  `CLOCK_REALTIME`-based; `CanReader` samples a one-time
  `monotonic() - time()` offset at the first frame and applies it after.
  Assumes `CLOCK_REALTIME` doesn't step during a run (use `chronyd`/
  `ntpd` slewing if that's a concern).
- **slcan timestamp quality**: `can_discover.py` reports
  `kernel_timestamping_expected: false` for slcan (a tty line discipline,
  no kernel CAN-frame timestamping) -- expect more jitter than gs_usb.
- **Block queue**: each sensor has its own bounded `queue.Queue`
  (`sensors[].sampling.queue_maxsize`), drops the oldest block if a
  consumer falls behind. Optional disk logging writes `.npz` files under
  `logging.raw_dir`, named `<sensor_name>_<t0_ns>.npz`.
- **`can_map.yaml` is intentionally not shipped** -- only
  `can_map.todo.yaml`. `can_reader.py` raises `CanMapError` if it's
  missing, rather than guessing signal mappings.
