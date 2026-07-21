"""Stubs `spidev` before any test imports vibration_monitor.py, which opens
the real SPI bus at module import time with no hardware fallback."""

import sys
import types


class _FakeSpiDev:
    def open(self, bus, device):
        pass

    def xfer2(self, data):
        return [0] * len(data)


if "spidev" not in sys.modules:
    fake_spidev = types.ModuleType("spidev")
    fake_spidev.SpiDev = _FakeSpiDev
    sys.modules["spidev"] = fake_spidev
