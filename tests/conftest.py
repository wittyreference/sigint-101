# ABOUTME: Keeps console tests off real Meshtastic interfaces.
# ABOUTME: Tests exercise the daemon boundary with an explicitly injected interface.

import pytest
from meshtastic import tcp_interface


@pytest.fixture(autouse=True)
def no_real_meshtastic(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("meshtasticd is unavailable in tests")

    monkeypatch.setattr(tcp_interface, "TCPInterface", refuse)
