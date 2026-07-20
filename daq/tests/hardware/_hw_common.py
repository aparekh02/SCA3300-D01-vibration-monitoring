#!/usr/bin/env python3
"""
_hw_common.py - shared gating/config helpers for the real-hardware test
suite in tests/hardware/. See HARDWARE_TESTING.md for how to run these.

Every test module in this directory self-skips (via unittest.SkipTest,
raised from setUpClass) unless explicitly opted into with an environment
variable -- so `python3 -m unittest discover -s tests` (the CI/sandbox-safe
suite) stays 100% safe to run anywhere, including this build environment,
which has no SPI device and no CAN interface at all.
"""

import os
import unittest
from pathlib import Path

import yaml

DAQ_DIR = Path(__file__).resolve().parent.parent.parent  # .../daq

RUN_HARDWARE_ENV = "DAQ_RUN_HARDWARE_TESTS"
RUN_SOAK_ENV = "DAQ_RUN_SOAK_TEST"


def require_hardware_tests():
    if os.environ.get(RUN_HARDWARE_ENV) != "1":
        raise unittest.SkipTest(
            f"set {RUN_HARDWARE_ENV}=1 to run this against real hardware -- see HARDWARE_TESTING.md"
        )


def require_soak_test():
    if os.environ.get(RUN_SOAK_ENV) != "1":
        raise unittest.SkipTest(
            f"set {RUN_SOAK_ENV}=1 to run the long-duration soak test -- see HARDWARE_TESTING.md"
        )


def load_config() -> dict:
    config_path = DAQ_DIR / os.environ.get("DAQ_CONFIG", "config.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f) or {}
