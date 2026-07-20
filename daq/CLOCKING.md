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

## Adding a second sensor: it's a config.yaml edit, not a code change

`config.yaml`'s `sensors:` list is exactly this mechanism exposed as
config. To add a second physical SCA3300 (a different bearing, a
different chip-select line), add another entry:

```yaml
sensors:
  - name: vibration_main
    type: sca3300
    spi: {bus: 0, device: 0, max_speed_hz: 2000000, mode: 1}
    sampling: {rate_hz: 2000, block_size: 4096, queue_maxsize: 8}
    realtime: {use_sched_fifo: true, required: false, priority: 80, cpu_core: 2, spin_margin_us: 50}

  - name: vibration_aux
    type: sca3300
    spi: {bus: 0, device: 1, max_speed_hz: 2000000, mode: 1}       # different CS line
    sampling: {rate_hz: 1000, block_size: 2048, queue_maxsize: 8}   # different rate is fine
    realtime: {use_sched_fifo: true, required: false, priority: 80, cpu_core: 3, spin_margin_us: 50}
```

`acquire.py`'s `Acquirer.__init__` loops over this list and calls
`hub.add_sensor(...)` once per entry (see `_register_sensor()`) -- nothing
else in `acquire.py`'s run loop, health logging, or Ctrl+C handling knows
or cares how many sensors are configured. Adding a genuinely different
sensor *type* (not just another SCA3300) means teaching
`_register_sensor()` how to build that type's `read_fn`; the `SensorHub`/
`RealTimeSampler` machinery underneath needs no changes for that either.

`tests/test_acquire.py::TestAcquirerMultiSensor` proves this end-to-end
against two independent fake SCA3300 devices, run concurrently from
nothing but two config dict entries.

### Getting blocks and health back out with more than one sensor

```python
acquirer = Acquirer(cfg)     # cfg["sensors"] has 2+ entries
acquirer.start()
block_main = acquirer.get_block("vibration_main", timeout=5.0)
block_aux = acquirer.get_block("vibration_aux", timeout=5.0)
acquirer.health_status()     # {"vibration_main": {...}, "vibration_aux": {...}}
```

With exactly one sensor configured, the `name` argument can be omitted
(`get_block()`, `health_status()`) -- there's only one thing it could mean.

### Aligning two vibration sensors' blocks (not just CAN RPM)

Both blocks' `t0_ns` are on `hub.clock`'s axis, so build a `(t, value)`
series from one block and reuse `align.py` -- the same interpolation seam
already used for CAN RPM -- to project it onto the other block's grid:

```python
from align import align_block

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
```

The key point: `vibration_main` and `vibration_aux` run on two independent
threads at two independent rates, either one can fault/reinit without
touching the other, and yet their block timestamps compose directly with
`align.py` -- no per-pair synchronization code was needed.

## GIL and concurrent high-rate sensors -- a measured caveat, not a guess

`Ticker.wait_for_next_tick()` spends the tail of each tick busy-waiting
(`while now() < deadline: pass`) for sub-millisecond precision. That loop
holds the GIL; when two sensors' sampler threads are both doing this at
2kHz on a shared (not `SCHED_FIFO`, not `isolcpus`-isolated) core, they
compete for it.

Measured on this build's dev sandbox (4 shared vCPUs, no real-time
scheduling, no CPU isolation -- i.e. *not* the target Pi setup):

| Scenario | intervals outside +-5% of 500us |
|---|---|
| One 2kHz sensor, no contention | ~8% (virtualization/scheduling jitter alone) |
| Two 2kHz sensors, concurrent | ~20-40% typically, spiking past 60% on a noisier run |

Reducing `spin_margin_us` (less time spent in the tight spin, more in a
real `time.sleep()` that actually releases the GIL) gives a modest
improvement, which is why the default was lowered from 100 to 50us -- but
it does not close the gap. This is not a bug to "fix" in software; it's
the GIL doing what the GIL does. `tests/test_clock.py`'s
`TestConcurrentHighRateSensors` runs this exact scenario on every test
run so the behavior stays visible rather than silently regressing further
without anyone noticing.

What this means in practice:
- On the real Pi, with `SCHED_FIFO` (`realtime.use_sched_fifo: true`) and
  each sensor pinned to its own `isolcpus`-isolated core
  (`realtime.cpu_core`), this contention mode mostly goes away -- each
  sampler gets a core to itself instead of time-slicing with another
  Python thread. That configuration can only be validated on real
  hardware, which is exactly what `tests/hardware/test_acquire_soak.py`
  is for (see `HARDWARE_TESTING.md`).
- If you need several sensors at full 2kHz on a Pi without enough
  isolated cores to give each one its own, this is the concrete signal to
  reach for the documented MCU front-end fallback (README "MCU front-end
  fallback") instead of trying to tune Python threading further.
- `realtime.required: true` (see config.yaml) at least turns "silently
  degraded to normal scheduling" into a loud startup failure, so this
  contention mode is never running invisibly.
