# ABOUTME: Verifies the test SBS port stays leased across workers.
# ABOUTME: Checks the fixture while its listening port is free for the ADS-B stand-in.

import fcntl

import pytest


def test_adsb_port_fixture_holds_its_shared_lease(adsb_ports, tmp_path_factory, request):
    root = tmp_path_factory.getbasetemp()
    if hasattr(request.config, "workerinput"):
        root = root.parent
    leases = root / "sbs-port-leases"
    leases.mkdir(exist_ok=True)

    with (leases / f"{adsb_ports}.lock").open("a") as contender:
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
