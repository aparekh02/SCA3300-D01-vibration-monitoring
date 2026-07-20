# daq/ - Sensor Acquisition & Discovery Layer

Two things live here, in build order:

1. **Verification/discovery tools** (`probe_sca3300.py`, `can_discover.py`) --
   run these first, on the real Pi/hardware, before trusting anything else.
2. **A deterministic acquisition path** (`sca3300.py`, `acquire.py`,
   `can_reader.py`, `align.py`) that produces evenly-sampled vibration
   blocks and a time-aligned RPM/load/torque series for a later analytics
   stage to consume, built on a **shared clock** (`clock.py`) so more
   sensors can be added later, each running concurrently on its own
   thread but sharing one timebase -- see `CLOCKING.md`.

**No FFT, order tracking, or health/diagnostics scoring is implemented
here** -- that's a separate, later task. This folder only acquires and
aligns data.

This folder does not touch or depend on anything in `src/`, `data/`, or
`archive/` at the repo root; those are a separate, already-working 100Hz
prototype for the same sensor and were left untouched.

This is a hardware-facing system layer, meant to run as a standing service
on the Pi that other software (analytics, dashboards, alerting) builds on
top of -- not a script someone launches by hand each time. See "Deploying
to a Raspberry Pi" below for the production install (systemd service, boot
config, upload/update commands) and "Integration contract for other
software" for what other code on the same Pi can actually depend on.

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
│   ├── test_align.py        # align.py interpolation correctness
│   ├── test_sca3300.py      # CRC/frame/startup/CRC-error-recovery, via fakes.py
│   ├── test_j1939.py        # 29-bit ID decode, PGN/SPN signal extraction
│   ├── test_can_reader.py   # can_map loading, message matching, timestamp offset
│   ├── test_can_discover.py # adapter detection, bus analysis, can_map.todo.yaml schema
│   ├── test_probe_sca3300.py# gravity/CRC-burst/timing-characterization logic
│   ├── test_clock.py        # SharedClock/Ticker/RealTimeSampler/SensorHub
│   ├── test_acquire.py      # Acquirer end-to-end (incl. multi-sensor), via fakes.py
│   └── hardware/             # real-hardware-only tests, self-skip without opt-in env var
│       ├── test_sca3300_hardware.py
│       ├── test_can_hardware.py
│       ├── test_acquire_soak.py
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
  (`can_discover.py` will tell you the actual driver/bitrate situation --
  don't assume 250000 without confirming.)

This section is for iterating on the code directly on a Pi you already
have shell access to. **For a production install other software will run
against, see "Deploying to a Raspberry Pi" below** -- that's the one with
the systemd service, boot-time CAN bring-up, and the actual upload/update
commands.

### Required privileges

- **SPI**: the user running these scripts needs read/write access to
  `/dev/spidev*` (member of the `spi` group on most Raspberry Pi OS images,
  or run as root).
- **Real-time scheduling** (each sensor's sampler thread): `SCHED_FIFO`
  requires root or `CAP_SYS_NICE`. Without it, the default
  (`sensors[].realtime.required: false`) logs a warning and runs at normal
  scheduling -- it still works, just with weaker timing guarantees under
  system load. This is no longer a silent degradation, though: every
  sampler's `health_status()` reports `sched_fifo_active` / `cpu_pinned`
  booleans regardless of the warning log, and setting
  `sensors[].realtime.required: true` turns a denial into a hard startup
  failure instead of a log line easy to miss. Grant the capability instead
  of running as root where possible:
  ```bash
  sudo setcap cap_sys_nice+ep $(readlink -f $(which python3))
  ```
- **CPU isolation** (recommended, not required): to give a sampler thread
  a core with minimal OS jitter, isolate a core from the general scheduler
  by adding to `/boot/cmdline.txt` (or `/boot/firmware/cmdline.txt` on
  newer Raspberry Pi OS):
  ```
  isolcpus=3 nohz_full=3 rcu_nocbs=3
  ```
  then reboot and set that sensor's `realtime.cpu_core: 3` in
  `config.yaml`. Adjust the core number for your Pi model (leave core 0
  for the OS), and give each concurrently-running sensor its own isolated
  core if you can -- see CLOCKING.md "GIL and concurrent high-rate
  sensors" for why that matters more than it might seem for more than one
  sensor at a high rate.
- **CAN**: bringing an interface up (`ip link set ... up`) requires
  `CAP_NET_ADMIN` (typically via `sudo`); reading/writing frames once it's
  up does not.

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

This is a hardware-facing acquisition layer other software (analytics,
dashboards, alerting -- whatever consumes vibration blocks and RPM) is
meant to run on top of, not just a script you launch by hand. That means
treating it like firmware: a fixed install location, a system service
that starts at boot and restarts on crash, boot-time CAN bring-up, and a
documented, stable surface other software can actually depend on. The
`daq/deploy/` directory has everything this section installs.

**Target**: Raspberry Pi OS (Bookworm or newer) on a Pi with the SCA3300
wired over SPI and a USB-CAN adapter attached, Python 3.11+.

### 1. Prepare the Pi (one-time, before any code goes on it)

```bash
sudo raspi-config   # Interface Options -> SPI -> enable, then reboot
```

Wire the SCA3300 to the SPI bus/CS line `config.yaml` will reference, and
plug in the USB-CAN adapter. Confirm both are visible before going further:

```bash
ls /dev/spidev*                 # should list e.g. /dev/spidev0.0
ip link show                    # should list a CAN interface, e.g. can0
```

If `ip link show` doesn't show a CAN interface at all, check
`dmesg | tail` for the adapter's driver load messages before assuming
anything about config -- that's a wiring/driver problem, not a software one.

### 2. Upload the code

From your dev machine, with this repo checked out locally. This deploys
**only `daq/`** (not the rest of this monorepo) to a fixed, documented
location, `/opt/daq` -- that's the path the systemd units in `daq/deploy/`
assume, so don't relocate it without editing them.

```bash
# Recommended: rsync just the daq/ subtree, excluding local test/build
# artifacts that shouldn't travel with a deploy.
rsync -avz --delete \
    --exclude '__pycache__' --exclude '.pytest_cache' \
    --exclude 'data/raw/*.npz' --exclude 'venv' \
    ./daq/ pi@<pi-host>:/opt/daq/
```

If your team instead tracks this repo's git history directly on the Pi
(e.g. to `git log`/`git blame` in place), clone the whole repo and treat
`/opt/daq` as a symlink into it instead:

```bash
ssh pi@<pi-host> "git clone --branch <branch> <repo-url> /opt/vibration-monitoring && \
                   sudo ln -s /opt/vibration-monitoring/daq /opt/daq"
```

Either way, `/opt/daq` should end up containing this folder's contents,
owned by whatever user will run `install.sh` next (that user needs
`sudo`; `install.sh` itself creates the actual service account).

### 3. Install as a system service

```bash
ssh pi@<pi-host>
cd /opt/daq/deploy
sudo ./install.sh
```

`install.sh` is idempotent (safe to re-run after every update) and:
- creates an unprivileged `daq` system user/group and gives it ownership
  of `/opt/daq`,
- creates a venv at `/opt/daq/venv` and `pip install -e`s this package
  into it (see "Integration contract for other software" below for what
  that buys other code on the same Pi),
- installs `99-daq-hardware.rules` (SPI device permissions for the `daq`
  group) and reloads udev,
- installs and enables `daq-can0-up.service` (brings `can0` up at boot at
  the bitrate hardcoded in that unit -- **keep it in sync with
  `config.yaml`'s `can.bitrate` by hand**, systemd can't read the YAML)
  and `daq-acquire.service` (runs `acquire.py` as the `daq` user, restarts
  on failure, grants `CAP_SYS_NICE` via `AmbientCapabilities` so
  `SCHED_FIFO` works without running as root -- see "Required privileges"
  above for why that matters).

```bash
systemctl status daq-acquire.service     # confirm it's running
journalctl -u daq-acquire.service -f     # tail its logs (health, blocks emitted, errors)
```

### 4. Verify before relying on it

Installing the service is not the same as validating the hardware. Before
treating a deployment as live:

```bash
# on the Pi, service stopped so it isn't fighting over the SPI/CAN devices
sudo systemctl stop daq-acquire.service
cd /opt/daq && source venv/bin/activate
python3 probe_sca3300.py --duration 60
python3 can_discover.py --duration 60
# review can_map.todo.yaml, confirm entries against a real spin-up, save as can_map.yaml
DAQ_RUN_HARDWARE_TESTS=1 python3 -m unittest tests.hardware.test_sca3300_hardware tests.hardware.test_can_hardware -v
sudo systemctl start daq-acquire.service
```

See `HARDWARE_TESTING.md` for the full test suite, including the
long-running soak test that certifies the actual Task 2 acceptance
criterion (`DAQ_RUN_SOAK_TEST=1`).

### 5. Updating a deployed instance

```bash
rsync -avz --delete --exclude '__pycache__' --exclude 'data/raw/*.npz' --exclude 'venv' \
    ./daq/ pi@<pi-host>:/opt/daq/
ssh pi@<pi-host> "cd /opt/daq/deploy && sudo ./install.sh && sudo systemctl restart daq-acquire.service"
```

`--exclude venv` above matters: without it, an rsync `--delete` would wipe
out the Pi's installed virtualenv along with the source, since `venv/`
only exists on the Pi, not in your local checkout.

### 6. Uninstalling

```bash
sudo systemctl disable --now daq-acquire.service daq-can0-up.service
sudo rm /etc/systemd/system/daq-acquire.service /etc/systemd/system/daq-can0-up.service
sudo rm /etc/udev/rules.d/99-daq-hardware.rules
sudo systemctl daemon-reload
sudo userdel daq   # only if no other service or data still needs that account
```

---

## Integration contract for other software

What another team's software running on the same Pi can actually depend
on today -- and, just as importantly, what it can't yet:

- **Python import surface**: after `pip install -e /opt/daq` (which
  `install.sh` already does into `/opt/daq/venv` -- point another
  service's own venv at the same `-e /opt/daq` install, or activate that
  venv directly) these modules are importable from anywhere:
  `sca3300`, `clock` (`SharedClock`/`Ticker`/`RealTimeSampler`/
  `SensorHub`), `align`, `can_reader`, `j1939`, and `acquire` (for its
  `Acquirer` class). This is the same set `pyproject.toml` declares under
  `[tool.setuptools] py-modules`. `probe_sca3300.py`/`can_discover.py` are
  deliberately not part of this surface -- they're verification CLIs, run
  them as scripts, not imports.
- **On-disk vibration blocks**: with `logging.write_blocks_to_disk: true`
  in `config.yaml`, each sensor writes `<raw_dir>/<sensor_name>_<t0_ns>.npz`
  containing `samples` (an `(n, 3)` array, columns X/Y/Z in g),
  `t0_ns` (monotonic timestamp of the first sample), `sample_rate_hz`, and
  `missed_in_block`. There's no push/pub-sub mechanism for these files
  today -- a consumer needs to poll or watch `raw_dir` itself (e.g. with
  `watchdog` or a simple directory poll).
- **In-process-only today (not yet on disk)**: the CAN RPM/load/torque
  series (`CanReader.series_snapshot(name)`) and live health status
  (`Acquirer.health_status()`) are currently only available to code
  running in the *same process* as `acquire.py` -- there is no file or
  socket export of either yet. A separate consumer process can currently
  only get the raw vibration blocks above, not aligned RPM alongside them.
  If your software needs cross-process access to either, that's a
  reasonable follow-up (e.g. periodically appending CAN series to disk,
  or a small local metrics/status endpoint) but isn't built -- don't
  assume it exists.
- **`align.py` is the seam, not a finished pipeline**: it's built and unit
  tested (linear interpolation of a `(t, value)` series onto a block's
  sample window) but nothing in `acquire.py` calls it automatically today.
  Consuming code that wants vibration-block-aligned RPM currently has to
  call `align_block()` itself with a vibration block and a CAN series --
  see `CLOCKING.md`'s worked example.
- **Versioning**: `pyproject.toml` currently pins `version = "0.1.0"` and
  there's no changelog yet -- if other software starts depending on this
  package, bumping that version on breaking changes (and noting them
  somewhere) is a reasonable next step, not something already in place.

### Tests

```bash
cd daq
python3 -m unittest discover -s tests
```

This is the hardware-free suite covered below -- it also includes
`tests/hardware/`, but those self-skip unless explicitly opted into (see
`HARDWARE_TESTING.md`), so this command is always safe to run, including
in this build environment. Once you have the real Pi/sensor/CAN adapter,
run `HARDWARE_TESTING.md`'s suite too -- it's the only thing that actually
certifies real-hardware behavior rather than protocol/logic correctness.

None of these require real hardware. `tests/fakes.py` implements a small
SCA3300 protocol simulator (independent CRC-8 implementation, so a bug in
`sca3300.py`'s own CRC code can't accidentally pass a test that checks
itself) that reproduces the sensor's pipelined off-frame response
behavior, including on-demand CRC-error injection at an exact transfer
index -- used to test `sca3300.py`'s error detection and `acquire.py`'s
reinit-on-error path end to end. CAN-side tests use synthetic
`can.Message`s and hand-built J1939 IDs rather than a real bus (this
sandbox has no `vcan`/`ip` tooling to bring up a virtual SocketCAN
interface -- if your dev machine has `vcan`, wiring `can_discover.py`'s
`sniff_bus()` against a real `vcan0` would be a natural next test to add).
`tests/test_clock.py` covers the shared-clock/multi-sensor machinery
directly, including a test that a fault in one sensor's read loop does not
stop a second, concurrently-running sensor.

What each file covers:
- `test_sca3300.py` -- CRC/frame construction against 3 cross-referenced
  known-good frames, startup success/failure paths, gravity-accurate
  reads, CRC-error detection and recovery via reinit.
- `test_j1939.py` -- 29-bit ID -> PGN/source-address decomposition
  (broadcast and peer-to-peer), SPN byte extraction, edge cases.
- `test_can_reader.py` -- `can_map.yaml` loading (missing-file error,
  unconfirmed signals skipped), J1939/raw message matching, the
  wall-clock-to-monotonic timestamp offset.
- `test_can_discover.py` -- adapter-type detection (mocked `ip`/`dmesg`),
  bus analysis (rate/candidate calculation, monotonicity check), and that
  the generated `can_map.todo.yaml` schema matches what `can_reader.py`
  actually expects (this exact mismatch was caught and fixed during
  development -- see git history).
- `test_probe_sca3300.py` -- gravity check pass/fail, CRC burst pass rate,
  timing characterization shape and target-rate tracking.
- `test_clock.py` -- deadline-grid alignment across different rates on one
  clock, missed-deadline resync, block assembly/health tracking, two
  concurrent sensors on one `SensorHub` staying independent,
  `realtime.required` actually raising on a denied SCHED_FIFO request (and
  rolling back any sensors already started), the O(1)-eviction regression
  guard for the rolling health window, and a measured (not asserted-away)
  characterization of GIL contention between two concurrent 2kHz sensors
  -- see CLOCKING.md "GIL and concurrent high-rate sensors" for the actual
  numbers this produced.
- `test_acquire.py` -- the SCA3300-to-generic-sampler adapter, a full
  `Acquirer` start/read-block/stop cycle including optional disk logging,
  and a config-driven two-sensor scenario proving `sensors:` list entries
  alone (no extra code) are enough to run two independent SCA3300 units
  concurrently.
- `test_align.py` -- linear interpolation correctness, clamping outside
  the series' range, and a full block-alignment scenario.

---

## Datasheet assumptions -- what's confirmed vs. still needs a check

This build environment could not reach a browser-rendered copy of the
primary Murata SCA3300-D01 PDF directly (fetches to the Mouser/Murata/LCSC
PDF hosts returned 403 from here). Instead, every SPI-protocol constant
below was **cross-verified against three independent sources that all
agree**, rather than invented:

1. This repo's own already-tested driver, `src/vibration_monitor.py`,
   whose exact frame bytes produced the real hardware output logged in
   `example_run.md` (`RS after startup: 01`, etc.).
2. Murata's official Linux kernel IIO driver,
   `drivers/iio/accel/sca3300.c` (upstream `torvalds/linux`), which
   documents the CRC-8 polynomial, register map, and per-mode scale/LPF
   tables in its source comments.
3. The `algebratech/sca3300-driver` Python reference implementation, which
   contains the literal 32-bit command frames as hex constants.

`daq/sca3300.py` doesn't hardcode those hex frames -- it builds each frame
from a register address + read/write bit and computes the CRC live, then
this build verified programmatically (see commit) that every frame it
produces matches all three sources' literal bytes exactly (SW_RESET,
mode-1 select, STATUS read, ACC_X/Y/Z read, WHOAMI read).

### Confirmed (cross-referenced, byte-exact match across all 3 sources)

- SPI mode 0 (CPOL=0, CPHA=0), 32-bit frames, MSB-first.
- CRC-8: polynomial `0x1D`, init `0xFF`, computed over the first 3 bytes of
  the frame, transmitted value is the bitwise NOT of the raw result.
- Frame byte 0 = `(write << 7) | (register_address << 2)` for requests;
  response byte 0's low 2 bits are the RS (return status) field.
- Register addresses: `ACC_X=0x01`, `ACC_Y=0x02`, `ACC_Z=0x03`,
  `STATUS=0x06` ("Summary Status"), `MODE=0x0D`, `WHOAMI=0x10`
  (expected value `0x51`).
- Software reset = write `0x0020` to the MODE register (bit 5).
- Mode select = write `0x0000`/`0x0001`/`0x0002`/`0x0003` to MODE register
  for modes 1/2/3/4 respectively.
- **Mode 1**: 2700 LSB/g sensitivity, 70Hz first-order LPF (matches the
  brief's "Default Mode 1 = +/-3g full-scale, 70Hz LPF"). This is the only
  mode `acquire.py`/`probe_sca3300.py` are designed and tested against.
- RS = `0b11` means error (all 3 sources agree). RS = `0b01` is the value
  this repo's own driver expects and observes immediately after a correct
  startup (see `example_run.md`).

### NOT independently confirmed -- verify before relying on them

- **TEMP register address** (`0x05` in `sca3300.py`): inferred from
  register-map ordering in community references, not verified against the
  primary datasheet table in this environment. `read_temp_raw()` returns
  the validated raw frame only -- no raw-to-Celsius formula is implemented,
  since that constant could not be confirmed either.
- **STATUS register bit-level semantics**: `read_status()` reports the raw
  16-bit value, RS, and a bit array (`bit0`..`bit8`), but individual bit
  *names/meanings* beyond "any bit set = not clean" are not implemented,
  since the exact bit table (which bit is X-axis saturation vs. clock
  error vs. power-on, etc.) could not be confirmed here. `probe_sca3300.py`
  prints which raw bits are set so a human can cross-reference the actual
  datasheet's Status Summary register table.
- **RS values `0b00` and `0b10`**: only `0b11` (error) and `0b01`
  (post-startup, per this repo's own tested behavior) are used in logic
  anywhere; the other two are treated as "not an error" but their precise
  meaning (stale data vs. normal-with-same-value, etc.) isn't asserted.
- **Modes 2-4 g-range**: sensitivity (LSB/g) and LPF are cross-referenced
  and consistent across all 3 sources, but the exact +/-g full-scale range
  for modes 2-4 is not confirmed (only Mode 1's +/-3g is, from the brief
  itself + cross-reference). Irrelevant unless you change a sensor's
  `spi.mode` in `config.yaml` away from 1.
- **Exact minimum inter-frame idle time**: the Linux driver applies a 10us
  SPI delay between requests; `sca3300.py` doesn't add an explicit delay
  (two separate `spidev.xfer2()` calls already toggle CS, which should
  satisfy this), but this hasn't been scope-verified against real
  hardware. If `probe_sca3300.py`'s CRC pass rate is below ~100%, check
  this first.
- **Startup settle timings**: the brief's own guidance is "~15ms" after
  mode select; this repo's own tested driver used 5ms after reset / 20ms
  after mode select and worked. `sca3300.py` defaults to those tested
  values (`start_up(post_reset_delay_s=0.005, post_mode_delay_s=0.020)`)
  but both are parameters -- tighten or loosen them once you have a real
  board to check settle behavior against.
- **J1939 SPN byte layouts** (`j1939.py: KNOWN_SIGNALS`): EEC1 SPN190
  (Engine Speed, 0.125 rpm/bit, bytes 4-5), SPN513 (Actual Engine %Torque,
  1%/bit, offset -125%, byte 3), and EEC2 SPN92 (Engine % Load At Current
  Speed, 1%/bit, byte 3) follow the commonly published SAE J1939-71 byte
  layout used by most open engine-ECU DBC files. This is **not** a
  guarantee for any specific vessel's ECU -- it's exactly why
  `can_discover.py` surfaces candidates instead of hardcoding trust in
  them, and why `can_map.todo.yaml` requires `confirmed: true` (set by a
  human after checking a real spin-up/throttle change) before
  `can_reader.py` will use a signal at all.

---

## Task 1 findings

Both `probe_sca3300.py` and `can_discover.py` were built and smoke-tested
in this environment for logic correctness (CRC/frame construction verified
byte-for-byte against three independent references; J1939 ID/PGN decode and
signal extraction verified with synthetic test vectors; `--help`, config
loading, and graceful-failure paths for missing hardware all exercised).
**Neither has been run against the actual SCA3300 board or the vessel's CAN
bus**, since this build environment has no SPI device or CAN adapter
attached. Run both on the real Pi and keep their `*_result.json` output for
the record; nothing here should be treated as validated against real
hardware until that's done.

**`tests/hardware/` turns exactly that validation into real, runnable
tests** (pass/fail assertions against real hardware, not just a report to
eyeball) -- see `HARDWARE_TESTING.md` for what each one certifies and how
to run them. They're gated behind an environment variable so the default
`python3 -m unittest discover -s tests` suite (which this build environment
*can* run) stays hardware-free and always safe.

What to look for when you do:
- `probe_sca3300.py`: CRC pass rate should be ~100%; the gravity check
  should show exactly one axis near +/-1g; the timing report's `p99`
  interval and `missed_count` are the numbers that decide whether Task 2's
  2kHz target is achievable in pure Python on this Pi (see fallback below).
- `can_discover.py`: confirm the adapter `kind` it reports (native
  SocketCAN vs. slcan) matches what you expect from the adapter's actual
  chipset, and that at least one EEC1/EEC2 candidate shows up if the bus
  is J1939. If the bus is proprietary, `can_map.todo.yaml` will come back
  with no candidates -- fill it in manually from the observed ID table in
  `can_discover_result.json`.

---

## MCU front-end fallback (not built -- document only)

Per the brief: if `probe_sca3300.py`'s timing characterization shows the Pi
cannot hold 2kHz within +/-5% jitter (`missed_count` or `p99` outside
target), **do not force it**. The recommended path is a small
microcontroller front-end (e.g. RP2040 or STM32) that:
- Samples the SCA3300 over SPI on a hardware timer at a true 2kHz
  (independent of any OS scheduling jitter).
- Buffers evenly-sampled blocks (matching this repo's `block_size`
  convention) and streams them to the Pi over USB-serial or USB-CDC as
  fixed-size binary frames, each tagged with an MCU-side monotonic
  timestamp for the first sample.
- The Pi-side would then need a thin replacement for `acquire.py`'s
  sampling loop that reads framed blocks from the MCU's serial port
  instead of driving SPI directly -- `align.py`, `can_reader.py`, and the
  block/health data model would be unaffected, since they only depend on
  receiving `(t0, samples[N,3])` blocks, not on how they were produced.

This is a TODO, not implemented -- `acquire.py` currently always drives the
SPI bus directly from the Pi's own real-time thread.

---

## Design notes / TODOs

- **SPI read pattern**: `sca3300.py` reads each axis with a
  request-then-NOP pair (2 SPI transfers per register) rather than a fully
  rolling pipeline across the whole sample sequence. This matches the
  already-tested pattern in `src/vibration_monitor.py` and leaves
  comfortable margin inside the 500us/sample budget at the configured SPI
  clock. A leaner 1-transfer-per-tick rolling pipeline (deferred-by-one
  reply) is possible if `probe_sca3300.py`'s timing numbers ever show
  Python/spidev call overhead -- not raw SPI transfer time -- is the
  bottleneck; noted here rather than built preemptively.
- **CAN timestamp alignment** (`can_reader.py`): python-can's SocketCAN
  backend timestamps frames from the kernel (`SO_TIMESTAMP`,
  `CLOCK_REALTIME`-based), while the vibration path's timebase is
  `time.monotonic()`. `CanReader` samples a one-time
  `monotonic() - time()` offset at the first received frame and applies it
  to every subsequent message timestamp. This assumes `CLOCK_REALTIME`
  doesn't step (e.g. an NTP correction) during a run; if that's a concern
  on your Pi, run `chronyd`/`ntpd` in a mode that slews rather than steps,
  or re-sample the offset periodically (not implemented here, since it
  wasn't in scope for Task 2's alignment plumbing).
- **slcan timestamp quality**: `can_discover.py` reports
  `kernel_timestamping_expected: false` for slcan adapters, since slcan is
  a tty line discipline without kernel CAN-frame timestamping;
  `can_reader.py` will still run against an slcan bus, but expect more
  jitter in the resulting RPM series than with a native gs_usb adapter.
- **Block queue**: each sensor gets its own bounded in-memory
  `queue.Queue` (`sensors[].sampling.queue_maxsize` in config) and drops
  its oldest block if a consumer falls behind, logging a warning.
  Optional disk logging (`logging.write_blocks_to_disk`) writes each
  block as an `.npz` under `logging.raw_dir`, named
  `<sensor_name>_<t0_ns>.npz`.
- **`can_map.yaml` is intentionally not shipped** -- only
  `can_map.todo.yaml` (a template/example) is. `can_reader.py` raises a
  clear `CanMapError` if `can_map.yaml` is missing, rather than guessing at
  signal mappings.
