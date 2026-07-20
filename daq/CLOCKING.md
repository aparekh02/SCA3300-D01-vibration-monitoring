# Clocking: running more than one sensor concurrently but separately

`clock.py` lets additional sensors be added later without each one
inventing its own timing loop or drifting onto its own private time axis.

## The requirement

Adding a second vibration sensor (another SCA3300 on a different bearing,
a different accelerometer model, anything) means answering three things:

1. **Concurrently** -- both sensors' read loops run at the same time,
   each at its own rate, without blocking each other.
2. **Separately** -- a CRC error, a re-init, a full queue, or a crash in
   one sensor's loop must not stop or corrupt the other's.
3. **On the same clock** -- despite running independently, every sensor's
   timestamps need to be on one shared axis, or aligning sensor A's block
   against sensor B's becomes a research project instead of an `align.py`
   call.

`clock.py` answers all three: one `SharedClock`, one
`SensorHub.add_sensor(...)` per sensor, each sensor its own thread.

## The pieces

```
SharedClock  -- the one time source everything timestamps against
Ticker       -- fixed-rate deadlines anchored to the SharedClock's origin
HealthMonitor / Block -- sensor-agnostic jitter/health tracking + block data
RealTimeSampler -- generic "read one sample, assemble a block" loop
SensorHub    -- registers N RealTimeSamplers, all sharing one SharedClock
```

### SharedClock

```python
clock = SharedClock()          # origin_ns = time.monotonic_ns() right now
clock.now_ns()                 # current monotonic time, same axis as origin
```

Wrapping `time.monotonic_ns()` in a class (instead of every sampler
calling it directly) gives one shared "t=0" regardless of when each
sampler actually started, lets tests inject a fake hand-advanceable clock
(see `tests/test_clock.py`), and means only this class would need to
change if the timebase ever became a hardware/PTP clock instead.

It's still the *same physical clock* as everything else here already
uses -- `can_reader.py`'s CAN timestamp conversion (via `time.monotonic()`
directly) is automatically on the same axis as any `SensorHub`'s default
clock, no extra wiring needed.

### Ticker

```python
ticker = Ticker(clock, period_ns=500_000)   # 2kHz
deadline_ns, missed = ticker.wait_for_next_tick()
```

Deadlines are `origin_ns + k * period_ns`, not "now + period_ns" -- so a
2000Hz and a 1000Hz Ticker on the same clock always have every 1000Hz
deadline land exactly on a 2000Hz one too, instead of each freewheeling
from whenever its own thread started. On a missed deadline, the next one
is recomputed from "now" rather than queuing up the backlog.

### RealTimeSampler / SensorHub

```python
hub = SensorHub()
hub.add_sensor("vibration_main", read_fn=..., n_channels=3, rate_hz=2000, block_size=4096)
hub.add_sensor("vibration_aux",  read_fn=..., n_channels=3, rate_hz=1000, block_size=2048, cpu_core=2)
hub.start_all()
```

`RealTimeSampler` calls `read_fn() -> (values, valid)`, assembles blocks,
tracks health -- it has no idea what `read_fn` does (SPI, socket, mock),
which is what lets unrelated sensors share this mechanism. Each
`add_sensor()` gets its own thread, queue, `HealthMonitor`, and optional
pinned CPU core -- independent fault domains, all sharing `hub.clock` so
every block's `t0_ns` is directly comparable.

## Adding a second sensor: a config.yaml edit, not a code change

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

`Acquirer.__init__` loops over this list, calling `hub.add_sensor(...)`
once per entry (`_register_sensor()`) -- nothing else in `acquire.py`
knows or cares how many sensors are configured. A genuinely different
sensor *type* means teaching `_register_sensor()` how to build its
`read_fn`; `SensorHub`/`RealTimeSampler` need no changes.
`tests/test_acquire.py::TestAcquirerMultiSensor` proves this end-to-end
against two independent fake SCA3300 devices.

### Getting blocks/health back out

```python
acquirer = Acquirer(cfg)     # cfg["sensors"] has 2+ entries
acquirer.start()
block_main = acquirer.get_block("vibration_main", timeout=5.0)
block_aux = acquirer.get_block("vibration_aux", timeout=5.0)
acquirer.health_status()     # {"vibration_main": {...}, "vibration_aux": {...}}
```

With exactly one sensor configured, `name` can be omitted -- there's only
one thing it could mean.

### Aligning two vibration sensors' blocks (not just CAN RPM)

Both blocks' `t0_ns` are on `hub.clock`'s axis, so build a `(t, value)`
series from one block and reuse `align.py` -- the same seam used for CAN
RPM -- to project it onto the other's grid:

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
# CAN RPM aligns onto either grid exactly as before -- no changes needed:
# rpm_on_main_grid = align_block(block_main.t0_ns/1e9, len(block_main.samples),
#                                 block_main.sample_rate_hz, can_reader.series_snapshot("rpm"))
```

Two independent threads, at two independent rates, either can
fault/reinit without touching the other -- yet their timestamps compose
directly with `align.py`. No per-pair synchronization code was needed.

## GIL and concurrent high-rate sensors -- a measured caveat, not a guess

`Ticker.wait_for_next_tick()` busy-waits the tail of each tick for
sub-millisecond precision, which holds the GIL. Two sensors' threads both
doing this at 2kHz on a shared (not `SCHED_FIFO`, not isolated) core
compete for it.

Measured on this build's dev sandbox (4 shared vCPUs, no real-time
scheduling or isolation -- *not* the target Pi setup):

| Scenario | intervals outside +-5% of 500us |
|---|---|
| One 2kHz sensor, no contention | ~8% (virtualization jitter alone) |
| Two 2kHz sensors, concurrent | ~20-40% typically, spiking past 60% on a noisier run |

Reducing `spin_margin_us` (default lowered 100->50us) gives a modest
improvement but doesn't close the gap -- this isn't a software bug, it's
the GIL doing what the GIL does. `tests/test_clock.py`'s
`TestConcurrentHighRateSensors` runs this scenario on every test run so
it stays visible instead of silently regressing further.

In practice: on the real Pi, `SCHED_FIFO` + each sensor pinned to its own
`isolcpus`-isolated core (`realtime.cpu_core`) mostly eliminates this,
since each sampler gets a core instead of time-slicing with another
thread -- only verifiable on real hardware, via
`tests/hardware/test_acquire_soak.py`. If you need several sensors at
full 2kHz without enough isolated cores, that's the signal to reach for
the documented MCU front-end fallback instead of tuning Python threading
further. `realtime.required: true` at least turns a silent fallback into
a loud startup failure.
