# ABOUTME: Verifies that the test SBS and AIS ports stay leased across workers.
# ABOUTME: Checks the file locks while the receiver and child can still bind the ports.

import fcntl

import pytest


def _assert_lease_held(port, directory, tmp_path_factory, request):
    root = tmp_path_factory.getbasetemp()
    if hasattr(request.config, "workerinput"):
        root = root.parent
    leases = root / directory
    leases.mkdir(exist_ok=True)

    with (leases / f"{port}.lock").open("a") as contender:
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_adsb_port_fixture_holds_its_shared_lease(adsb_ports, tmp_path_factory, request):
    _assert_lease_held(adsb_ports, "sbs-port-leases", tmp_path_factory, request)


def test_ais_port_fixture_holds_its_shared_lease(ais_port, tmp_path_factory, request):
    _assert_lease_held(ais_port, "ais-port-leases", tmp_path_factory, request)
