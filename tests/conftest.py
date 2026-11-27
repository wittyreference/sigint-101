# ABOUTME: Keeps console tests off real UPS I2C adapters.
# ABOUTME: Battery tests can substitute register bytes at the SMBus boundary.

import sys
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def no_real_i2c(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise FileNotFoundError("the I2C bus is unavailable in tests")

    monkeypatch.setitem(sys.modules, "smbus2", SimpleNamespace(SMBus=refuse))
