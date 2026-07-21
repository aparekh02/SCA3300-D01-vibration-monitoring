"""
Stubs the `spidev` module before any test imports vibration_monitor.py.

vibration_monitor.py opens the real SPI bus at *module import time*
(spidev.SpiDev().open(0, 0)), which has no fallback for machines without
the hardware/driver -- there is no way to import it in a normal CI/test
environment otherwise. This is a pre-existing property of that module
(see NOTES.md), not something introduced here; the stub exists solely so
tests can import it and exercise its pure functions.
"""

import sys
import types


class _FakeSpiDev:
    def __init__(self):
        self.max_speed_hz = None
        self.mode = None

    def open(self, bus, device):
        pass

    def xfer2(self, data):
        return [0] * len(data)

    def close(self):
        pass


if "spidev" not in sys.modules:
    fake_spidev = types.ModuleType("spidev")
    fake_spidev.SpiDev = _FakeSpiDev
    sys.modules["spidev"] = fake_spidev
