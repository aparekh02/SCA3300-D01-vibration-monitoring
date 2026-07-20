# Clocking: running more than one sensor concurrently but separately

This describes `clock.py`, the piece that lets additional sensors be added
to the acquisition path later without each one inventing its own timing
loop or drifting onto its own private time axis.

## The requirement

Task 2 already has this shape: read a sensor at a fixed rate, on its own
thread, and stamp samples so a later stage can line them up against other
data (originally just CAN RPM, via `align.py`). Adding a second vibration
sensor (a second SCA3300 on another bearing, a different accelerometer
model, anything) means answering three things:

1. **Concurrently** -- both sensors' read loops must actually run at the
   same time, each at its own rate, without blocking each other.
2. **Separately** -- a CRC error, a re-init, a full queue, or a crash in
   one sensor's loop must not stop or corrupt the other's.
3. **On the same clock** -- despite running independently, every sensor's
   timestamps need to be numbers on one shared axis, or "align sensor A's
   block against sensor B's block" becomes a research project instead of
   an `align.py` call.

`clock.py` is the answer to all three: one `SharedClock`, one
`SensorHub.add_sensor(...)` per sensor, each sensor gets its own thread.

## The pieces

```
SharedClock  -- the one time source everything timestamps against
Ticker       -- fixed-rate deadlines anchored to the SharedClock's origin
HealthMonitor / Block -- unchanged from before, just sensor-agnostic now
RealTimeSampler -- generic "read one sample, assemble a block" loop
SensorHub    -- registers N RealTimeSamplers, all sharing one SharedClock
```

### SharedClock

```python
clock = SharedClock()          # origin_ns = time.monotonic_ns() right now
clock.now_ns()                 # current monotonic time, same axis as origin
```

Wrapping `time.monotonic_ns()` in a class (instead of every sampler calling
it directly) means:
- There's exactly one definition of "t=0" -- the moment the clock was
  created -- so two samplers started microseconds or minutes apart still
  share the same axis without a manual offset between them.
- Tests can hand `SharedClock(time_source=fake_clock)` a hand-advanceable
  fake instead of real wall-clock time, to test timing logic deterministically
  and instantly (see `tests/test_clock.py`).
- If this ever needs to become a hardware/PTP clock instead of
  `monotonic_ns()`, only `SharedClock` changes -- nothing downstream does.

It is still, deliberately, the *same physical clock* as everything else in
this repo already uses: `time.monotonic_ns()` / `time.monotonic()` is one
global monotonic counter per machine, so `can_reader.py`'s CAN timestamp
conversion (which uses `time.monotonic()` directly, not a `SharedClock`
instance) is automatically on the same axis as any `SensorHub`'s
`SharedClock()` default -- no extra wiring needed for CAN-to-vibration
alignment to keep working exactly as it did before this change.

### Ticker

```python
ticker = Ticker(clock, period_ns=500_000)   # 2kHz
deadline_ns, missed = ticker.wait_for_next_tick()
```

Deadlines are computed as `origin_ns + k * period_ns`, not "now +
period_ns" -- so a 2000Hz Ticker and a 1000Hz Ticker built from the *same*
clock always have every 1000Hz deadline land exactly on a 2000Hz deadline
too. Two sensors at different rates still end up on a common grid instead
of each freewheeling from whenever its own thread happened to start.

On a missed deadline, the next one is recomputed from "now" rather than
queuing up the backlog -- a slow tick doesn't cascade into permanently
running behind.

### RealTimeSampler

Generic fixed-rate loop: calls `read_fn() -> (values, valid)`, assembles
`block_size`-length blocks, tracks jitter/missed-sample/validity health via
`HealthMonitor`. It has no idea what `read_fn` actually does -- SPI, I2C, a
socket, a mock in a test -- which is exactly what lets unrelated sensors
share this one mechanism.

### SensorHub

The registration point:

```python
hub = SensorHub()
hub.add_sensor("vibration_main", read_fn=..., n_channels=3, rate_hz=2000, block_size=4096)
hub.add_sensor("vibration_aux",  read_fn=..., n_channels=3, rate_hz=1000, block_size=2048,
               cpu_core=2)  # a second isolated core, if available
hub.start_all()
```

Each `add_sensor()` call gets its own thread, its own queue, its own
`HealthMonitor`, and optionally its own pinned CPU core -- independent
fault domains. All of them share `hub.clock`, so every block's `t0_ns` is
directly comparable across sensors with no extra bookkeeping.

## Worked example: two vibration sensors + CAN RPM, all aligned

This is a runnable sketch (the second sensor's `read_fn` is faked here
since only one physical SCA3300 exists on this build; swap in a real
driver call for actual second hardware):

```python
import numpy as np
from clock import SensorHub
from sca3300 import SCA3300
from acquire import make_sca3300_read_fn
from align import align_block, interpolate_series

hub = SensorHub()

# Sensor 1: the real SCA3300, per config.yaml (bus 0 / CS 0), at 2kHz.
sca_main = SCA3300(bus=0, device=0, mode=1)
sca_main.start_up()
hub.add_sensor("vibration_main", make_sca3300_read_fn(sca_main),
               n_channels=3, rate_hz=2000, block_size=4096)

# Sensor 2: e.g. a second SCA3300 on CS1, at a different rate to show the
# grid-alignment guarantee -- replace with a real driver call.
sca_aux = SCA3300(bus=0, device=1, mode=1)
sca_aux.start_up()
hub.add_sensor("vibration_aux", lambda: (sca_aux.read_accel()[0], True),
               n_channels=3, rate_hz=1000, block_size=2048)

hub.start_all()

block_main = hub.get_block("vibration_main", timeout=5.0)
block_aux = hub.get_block("vibration_aux", timeout=5.0)

# Both blocks' t0_ns are on hub.clock's axis, so build an (t, value) series
# from the aux block and reuse align.py -- the same interpolation seam
# already used for CAN RPM -- to project it onto the main block's grid.
aux_rate = block_aux.sample_rate_hz
aux_series_x = [
    (block_aux.t0_ns / 1e9 + i / aux_rate, block_aux.samples[i, 0])
    for i in range(len(block_aux.samples))
]
aux_x_on_main_grid = align_block(
    t0=block_main.t0_ns / 1e9,
    n_samples=len(block_main.samples),
    sample_rate_hz=block_main.sample_rate_hz,
    series=aux_series_x,
)

# CAN RPM aligns onto either block's grid exactly as before -- CanReader's
# series already lives on this same monotonic axis (see SharedClock note
# above), no changes needed there.
# rpm_on_main_grid = align_block(block_main.t0_ns/1e9, len(block_main.samples),
#                                 block_main.sample_rate_hz, can_reader.series_snapshot("rpm"))

hub.stop_all()
```

The key point: `vibration_main` and `vibration_aux` run on two independent
threads at two independent rates, either one can fault/reinit without
touching the other, and yet their block timestamps compose directly with
`align.py` -- no per-pair synchronization code was needed.

## Where this plugs into `acquire.py`

`acquire.py`'s `Acquirer` class is now a thin wrapper: it creates a
`SensorHub`, registers the SCA3300 as the `"vibration"` sensor via
`make_sca3300_read_fn()`, and exposes the same `start()` / `stop()` /
`get_block()` / `health_status()` surface as before. Adding a real second
sensor to the CLI tool means calling `acquirer.hub.add_sensor(...)` with
that sensor's own `read_fn` before `acquirer.start()` -- everything else
(the run loop, health logging, Ctrl+C handling) is already sensor-count
agnostic since it only iterates over `hub`'s registered sensors implicitly
through `hub.start_all()` / `hub.stop_all()` / `hub.health()`.

`config.yaml` intentionally still describes a single `spi:` block, since
this build only has one physical sensor to validate against -- extending
it to a `sensors: [...]` list is a natural next step once a second sensor
exists to design the schema around, rather than guessing its config shape
now.
