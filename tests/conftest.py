# ABOUTME: Keeps console tests off real Meshtastic and UPS I2C interfaces.
# ABOUTME: Hardware tests substitute explicitly at either boundary.

import sys
from types import SimpleNamespace

import pytest
from meshtastic import tcp_interface


@pytest.fixture(autouse=True)
def no_real_meshtastic(monkeypatch):
    class NoRadioTCP(tcp_interface.TCPInterface):
        def __init__(self, *args, **kwargs):
            raise OSError("meshtasticd is unavailable in tests")

    monkeypatch.setattr(tcp_interface, "TCPInterface", NoRadioTCP)


@pytest.fixture(autouse=True)
def no_real_i2c(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise FileNotFoundError("the I2C bus is unavailable in tests")

    monkeypatch.setitem(sys.modules, "smbus2", SimpleNamespace(SMBus=refuse))
