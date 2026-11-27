# ABOUTME: Keeps console tests off real Meshtastic and UPS I2C interfaces.
# ABOUTME: ADS-B tests use temporary SBS ports away from the live console.

import socket
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


@pytest.fixture(autouse=True)
def adsb_ports(monkeypatch):
    """Use an ephemeral SBS port so the suite leaves the console's port alone."""
    from sigint101 import adsb

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(adsb, "SBS_PORT", port)
    return port
