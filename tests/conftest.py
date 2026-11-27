# ABOUTME: Keeps console tests off real Meshtastic interfaces.
# ABOUTME: Tests exercise the daemon boundary with an explicitly injected interface.

import socket
from contextlib import ExitStack

import pytest
from meshtastic import tcp_interface


@pytest.fixture(autouse=True)
def no_real_meshtastic(monkeypatch):
    class NoRadioTCP(tcp_interface.TCPInterface):
        def __init__(self, *args, **kwargs):
            raise OSError("meshtasticd is unavailable in tests")

    monkeypatch.setattr(tcp_interface, "TCPInterface", NoRadioTCP)


@pytest.fixture
def adsb_ports(monkeypatch):
    """Give launcher tests distinct ephemeral ports, never the real tool's ports."""
    from sigint101 import adsb

    with ExitStack() as sockets:
        ports = {}
        for attr in ("SBS_PORT", "RAW_OUT_PORT", "BEAST_OUT_PORT"):
            sock = sockets.enter_context(socket.socket())
            sock.bind(("127.0.0.1", 0))
            ports[attr] = sock.getsockname()[1]
            monkeypatch.setattr(adsb, attr, ports[attr], raising=False)
    return ports
