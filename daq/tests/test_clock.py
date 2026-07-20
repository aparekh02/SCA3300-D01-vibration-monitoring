#!/usr/bin/env python3
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clock import SharedClock, Ticker, HealthMonitor, RealTimeSampler, SensorHub


class FakeTimeSource:
    """A hand-advanceable monotonic-ns-like counter, so Ticker/SharedClock
    logic can be tested deterministically without real sleeping."""

    def __init__(self, start_ns: int = 0):
        self._now_ns = start_ns

    def __call__(self) -> int:
        return self._now_ns

    def advance(self, delta_ns: int):
        self._now_ns += delta_ns


class TestSharedClock(unittest.TestCase):
    def test_origin_is_time_of_construction(self):
        fake = FakeTimeSource(start_ns=1_000_000)
        clock = SharedClock(time_source=fake)
        self.assertEqual(clock.origin_ns, 1_000_000)
        self.assertEqual(clock.elapsed_ns(), 0)

        fake.advance(500)
        self.assertEqual(clock.elapsed_ns(), 500)
        self.assertEqual(clock.now_ns(), 1_000_500)


class TestTicker(unittest.TestCase):
    """These use a fake, hand-advanceable clock and deliberately never call
    wait_for_next_tick() from a state where it would need to busy-wait for
    the clock to advance on its own -- a fake clock that isn't advancing
    would spin forever in that branch (real time.sleep()/real elapsed time
    is what normally advances a real clock during the wait). Every call
    below either pre-advances the fake past the deadline (exercising the
    "missed" / resync branch, which never spins) or inspects the computed
    deadline directly without blocking."""

    def test_two_tickers_on_same_clock_share_a_grid(self):
        """A 2000Hz and a 1000Hz Ticker built from the same clock should
        have every 1000Hz deadline coincide exactly with a 2000Hz one --
        this is what lets two sensors at different rates still produce
        directly comparable timestamps."""
        fake = FakeTimeSource(start_ns=0)
        clock = SharedClock(time_source=fake)

        fast = Ticker(clock, period_ns=500_000)   # 2kHz
        slow = Ticker(clock, period_ns=1_000_000)  # 1kHz

        fast_deadlines = [fast._next_deadline_ns]
        for _ in range(3):
            fast_deadlines.append(fast_deadlines[-1] + fast.period_ns)

        slow_deadlines = [slow._next_deadline_ns]
        for _ in range(1):
            slow_deadlines.append(slow_deadlines[-1] + slow.period_ns)

        for d in slow_deadlines:
            self.assertIn(d, fast_deadlines)

    def test_missed_deadline_resyncs_instead_of_catching_up(self):
        fake = FakeTimeSource(start_ns=0)
        clock = SharedClock(time_source=fake)
        ticker = Ticker(clock, period_ns=1000)

        # Jump far past several missed periods before ever waiting, so the
        # very first call takes the non-blocking "missed" branch.
        fake.advance(10_500)
        deadline, missed = ticker.wait_for_next_tick()
        self.assertTrue(missed)

        # The next deadline should be the next grid point from "now", not
        # 1000ns after the missed one (which would still be in the past).
        next_deadline = ticker._next_deadline_ns
        self.assertGreater(next_deadline, fake())

    def test_no_missed_flag_when_on_time(self):
        """The not-missed (sleep + spin) branch inherently needs the clock
        to advance on its own during the wait, which only a real clock
        does -- covered here with a real SharedClock and a period short
        enough to keep the test fast, rather than with FakeTimeSource
        (which would spin forever waiting for itself to advance)."""
        clock = SharedClock()
        ticker = Ticker(clock, period_ns=1_000_000)  # 1kHz, ~1ms away
        _, missed = ticker.wait_for_next_tick()
        self.assertFalse(missed)


class TestHealthMonitor(unittest.TestCase):
    def test_stats_over_known_intervals(self):
        target_ns = 500_000
        health = HealthMonitor(target_ns, tolerance_frac=0.05)
        intervals = [500_000, 500_000, 500_000, 600_000, 400_000]  # last two exceed +-5%
        for i in intervals:
            health.record_interval(i)
        status = health.status()
        self.assertEqual(status["intervals_recorded"], 5)
        self.assertAlmostEqual(status["mean_us"], sum(intervals) / len(intervals) / 1000.0)
        self.assertEqual(status["missed_count"], 2)
        self.assertAlmostEqual(status["min_us"], 400.0)
        self.assertAlmostEqual(status["max_us"], 600.0)

    def test_invalid_sample_rate_tracked_independently_of_intervals(self):
        health = HealthMonitor(500_000)
        health.record_sample(True)
        health.record_sample(False)
        health.record_sample(False)
        status = health.status()
        self.assertEqual(status["samples_total"], 3)
        self.assertEqual(status["invalid_samples"], 2)
        self.assertAlmostEqual(status["invalid_rate"], 2 / 3)


class TestRealTimeSampler(unittest.TestCase):
    def test_produces_blocks_of_the_right_shape(self):
        clock = SharedClock()  # real clock -- this test runs a real (short) thread
        counter = {"n": 0}

        def read_fn():
            counter["n"] += 1
            return (counter["n"], counter["n"] * 2, counter["n"] * 3), True

        sampler = RealTimeSampler("test_sensor", read_fn, n_channels=3, rate_hz=1000,
                                   block_size=10, clock=clock, use_sched_fifo=False)
        sampler.start()
        try:
            block = sampler.get_block(timeout=5.0)
        finally:
            sampler.stop()

        self.assertIsNotNone(block)
        self.assertEqual(block.sensor_name, "test_sensor")
        self.assertEqual(block.samples.shape, (10, 3))
        self.assertEqual(block.missed_in_block, 0)
        # Each row should be (n, 2n, 3n) for consecutive n.
        for row in block.samples:
            self.assertAlmostEqual(row[1], row[0] * 2)
            self.assertAlmostEqual(row[2], row[0] * 3)

    def test_invalid_samples_are_nan_and_counted_as_missed(self):
        clock = SharedClock()
        calls = {"n": 0}

        def read_fn():
            calls["n"] += 1
            valid = calls["n"] % 2 == 0
            return (1.0, 1.0, 1.0), valid

        sampler = RealTimeSampler("flaky_sensor", read_fn, n_channels=3, rate_hz=2000,
                                   block_size=10, clock=clock, use_sched_fifo=False)
        sampler.start()
        try:
            block = sampler.get_block(timeout=5.0)
        finally:
            sampler.stop()

        self.assertEqual(block.missed_in_block, 5)
        nan_rows = sum(1 for row in block.samples if all(x != x for x in row))
        self.assertEqual(nan_rows, 5)

    def test_read_fn_exception_does_not_kill_sampler_thread(self):
        clock = SharedClock()
        calls = {"n": 0}

        def read_fn():
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated transient fault")
            return (1.0, 1.0, 1.0), True

        errors_seen = []
        sampler = RealTimeSampler("error_prone_sensor", read_fn, n_channels=3, rate_hz=2000,
                                   block_size=10, clock=clock, use_sched_fifo=False,
                                   on_error=lambda exc: errors_seen.append(exc))
        sampler.start()
        try:
            block = sampler.get_block(timeout=5.0)
        finally:
            sampler.stop()

        self.assertIsNotNone(block)  # the thread survived the exception and finished the block
        self.assertEqual(len(errors_seen), 1)
        self.assertEqual(block.missed_in_block, 1)


class TestSensorHub(unittest.TestCase):
    def test_two_sensors_run_concurrently_and_independently(self):
        hub = SensorHub()
        events = []
        lock = threading.Lock()

        def make_read_fn(name, fail_on_call=None):
            state = {"n": 0}

            def read_fn():
                state["n"] += 1
                with lock:
                    events.append((name, state["n"]))
                if fail_on_call is not None and state["n"] == fail_on_call:
                    raise RuntimeError(f"{name} fault")
                return (state["n"],), True

            return read_fn

        hub.add_sensor("a", make_read_fn("a"), n_channels=1, rate_hz=2000, block_size=5,
                        use_sched_fifo=False)
        hub.add_sensor("b", make_read_fn("b", fail_on_call=2), n_channels=1, rate_hz=1000, block_size=5,
                        use_sched_fifo=False)

        self.assertEqual(set(hub.sensors()), {"a", "b"})

        hub.start_all()
        try:
            block_a = hub.get_block("a", timeout=5.0)
            block_b = hub.get_block("b", timeout=5.0)
        finally:
            hub.stop_all()

        # Sensor "a" was never faulted and should have zero missed samples.
        self.assertIsNotNone(block_a)
        self.assertEqual(block_a.missed_in_block, 0)

        # Sensor "b" hit a synthetic fault but still completed its block --
        # i.e. sensor "a" faulting (it didn't) or "b" faulting did not stop
        # the other sensor's independent thread.
        self.assertIsNotNone(block_b)
        self.assertEqual(block_b.missed_in_block, 1)

        # Both sensors' timestamps are on the same shared clock.
        self.assertIs(hub._samplers["a"]._clock, hub.clock)
        self.assertIs(hub._samplers["b"]._clock, hub.clock)

        names_seen = {name for name, _ in events}
        self.assertEqual(names_seen, {"a", "b"})

    def test_health_reports_per_sensor_and_aggregate(self):
        hub = SensorHub()
        hub.add_sensor("a", lambda: ((1.0,), True), n_channels=1, rate_hz=2000, block_size=1000,
                        use_sched_fifo=False)
        hub.add_sensor("b", lambda: ((2.0,), True), n_channels=1, rate_hz=2000, block_size=1000,
                        use_sched_fifo=False)
        hub.start_all()
        try:
            hub.get_block("a", timeout=5.0)
        finally:
            hub.stop_all()

        single = hub.health("a")
        self.assertIn("samples_total", single)

        all_health = hub.health()
        self.assertEqual(set(all_health.keys()), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
