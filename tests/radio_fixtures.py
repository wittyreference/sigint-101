# ABOUTME: Keeps console tests off real Meshtastic and UPS I2C interfaces.
# ABOUTME: Leases loopback SBS ports so parallel tests never contend for one.

import errno
import fcntl
import random
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


@pytest.fixture(scope="session")
def sbs_port_leases(tmp_path_factory, request):
    root = tmp_path_factory.getbasetemp()
    # Local xdist workers get child directories of the controller's base directory.
    if hasattr(request.config, "workerinput"):
        root = root.parent
    leases = root / "sbs-port-leases"
    leases.mkdir(exist_ok=True)
    return leases


@pytest.fixture(autouse=True)
def adsb_ports(monkeypatch, sbs_port_leases):
    """Lease a port without occupying it when the test preflights or launches its child."""
    from sigint101 import adsb

    # Stay below the default Linux ephemeral range and away from the live SBS port (30003).
    for port in random.sample(range(15000, 30000), 32):
        with (sbs_port_leases / f"{port}.lock").open("a") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            # Take the lease before binding so a probe cannot briefly occupy another test's port.
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                    continue
            monkeypatch.setattr(adsb, "SBS_PORT", port)
            yield port
            return
    raise RuntimeError("could not lease an unused ADS-B test port")
