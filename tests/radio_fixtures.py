# ABOUTME: Keeps console tests off real Meshtastic and UPS I2C interfaces.
# ABOUTME: Leases loopback ports for ADS-B and AIS tests under xdist.

import errno
import fcntl
import random
import socket
import sys
from contextlib import contextmanager
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
def port_lease_root(tmp_path_factory, request):
    root = tmp_path_factory.getbasetemp()
    # Local xdist workers get child directories of the controller's base directory.
    if hasattr(request.config, "workerinput"):
        root = root.parent
    return root


@contextmanager
def _lease_loopback_port(leases, socktype):
    """Reserve a test port across workers while leaving it free for the decoder to bind."""
    leases.mkdir(exist_ok=True)
    # Stay below the default Linux ephemeral range and away from the live SBS port (30003).
    for port in random.sample(range(15000, 30000), 32):
        with (leases / f"{port}.lock").open("a") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            # Take the lease before binding so a probe cannot briefly occupy another test's port.
            with socket.socket(socket.AF_INET, socktype) as sock:
                if socktype == socket.SOCK_STREAM:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                    continue
            yield port
            return
    raise RuntimeError(f"could not lease an unused {socktype.name} test port")


@pytest.fixture(autouse=True)
def adsb_ports(monkeypatch, port_lease_root):
    from sigint101 import adsb

    with _lease_loopback_port(port_lease_root / "sbs-port-leases", socket.SOCK_STREAM) as port:
        monkeypatch.setattr(adsb, "SBS_PORT", port)
        yield port


@pytest.fixture
def ais_port(port_lease_root):
    with _lease_loopback_port(port_lease_root / "ais-port-leases", socket.SOCK_DGRAM) as port:
        yield port
