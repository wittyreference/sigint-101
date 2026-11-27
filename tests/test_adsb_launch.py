# ABOUTME: Exercises ADS-B startup and its fixed SBS endpoint without opening a radio.
# ABOUTME: Checks process ownership, SDR handoff, and reception records through the console.

import threading

import pytest

from sigint101 import adsb, childproc, receiver_lifecycle
from sigint101.events import read_events
from tests.child_fakes import Child
from tests.test_app import _client, _present, _StubSweeper


@pytest.fixture(autouse=True)
def _isolated_ports(adsb_ports):
    return adsb_ports


def _decoder():
    return adsb.AdsbDecoder(device=2, serial="adsb-test", radio_present=lambda: True,
                            which=lambda tool: tool)


def _records(path):
    return [event["payload"] for event in read_events(path / "events.jsonl")
            if event["kind"] == "reception"]


@pytest.mark.parametrize("fault", ["readiness-poll", "socket-setup-and-close"])
def test_adsb_post_spawn_fault_retains_child_until_reaped(tmp_path, monkeypatch, fault):
    can_reap = threading.Event()
    fault_raised = threading.Event()
    launches = []

    class RecoveringChild(Child):
        def poll(self):
            if fault == "readiness-poll" and not fault_raised.is_set():
                fault_raised.set()
                raise OSError("readiness poll failed")
            return super().poll()

        def kill(self):
            self.unkillable = not can_reap.is_set()
            super().kill()

    class BrokenSocket:
        closed = False
        def settimeout(self, _timeout):
            fault_raised.set()
            raise OSError("socket timeout setup failed")

        def shutdown(self, _how):
            pass

        def close(self):
            if not can_reap.is_set():
                raise OSError("socket close failed")
            self.closed = True

        def fileno(self):
            return -1 if self.closed else 1

    proc = RecoveringChild(unkillable=True)
    sock = BrokenSocket()

    def spawn(argv, **kwargs):
        proc.args = argv
        launches.append(proc)
        return proc

    def connect(*args, **kwargs):
        if fault == "readiness-poll":
            raise ConnectionRefusedError("socket not listening")
        return sock

    def verify_peer(_proc):
        from contextlib import nullcontext

        return nullcontext(lambda _sock: True)

    monkeypatch.setattr(adsb, "spawn", spawn)
    monkeypatch.setattr(adsb, "_sbs_peer", verify_peer, raising=False)
    monkeypatch.setattr(adsb.socket, "create_connection", connect)
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.02)
    receiver = _decoder()
    sweep = _StubSweeper([])
    with _client(tmp_path, probe=_present, adsb_factory=lambda: receiver,
                 sweeper_factory=lambda: sweep) as client:
        try:
            first = client.get("/api/adsb")
            assert first.status_code == 503 and fault_raised.is_set()
            run = receiver._run
            assert run is not None, "startup lost the child that still owned the SDR"
            assert run.proc.procs == (proc,)
            assert run.reader is None and not run.reaped
            assert first.json()["reason"] == run.failure
            assert ("transport could not close" if fault == "socket-setup-and-close"
                    else "survived kill") in run.failure
            assert client.get("/api/adsb").status_code == 503
            competing = client.get("/api/spectrum")
            assert competing.status_code == 503 and not sweep.running
            assert competing.json()["reason"] == run.failure
            assert launches == [proc] and _records(tmp_path) == []
        finally:
            can_reap.set()
        assert run.complete.wait(3) and run.reaped
        assert proc.sent[0] == "TERM" and set(proc.sent[1:]) == {"KILL"}
        assert len(proc.sent) >= 3
        assert sock.closed is (fault == "socket-setup-and-close")
        assert client.get("/api/spectrum").status_code == 200 and sweep.running
