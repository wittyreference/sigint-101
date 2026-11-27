# ABOUTME: Exercises SDR reservations and bounded shutdown through the console.
# ABOUTME: Uses process and logging gates instead of requiring a radio.

import select
import subprocess
import sys
import threading

from sigint101 import app, aprs, childproc
from tests.child_fakes import Child
from tests.test_app import _client, _present, _StubSweeper


def _aprs(proc):
    return aprs.AprsDecoder(spawn=lambda *_: proc, radio_present=lambda: True, which=lambda t: t)


def test_two_failed_teardown_claims_refuse_without_retrying_under_reservation(tmp_path):
    proc = Child(["aprs"], unkillable=True)
    decoder = _aprs(proc)
    with _client(tmp_path, probe=_present, aprs_factory=lambda: decoder,
                 sweeper_factory=lambda: _StubSweeper([])) as client:
        decoder.start()
        first = client.get("/api/spectrum")
        second = client.post("/api/ppm/measure")
        assert first.status_code == second.status_code == 503
        assert "survived kill" in first.json()["reason"]
        assert first.json()["reason"] == second.json()["reason"]
        assert proc.sent == ["TERM", "KILL"]
        assert not client.app.state.sweeper.running
        proc.unkillable = False
        proc.stubborn = False


def test_late_diagnostic_failure_cannot_hold_or_clear_a_new_claim(tmp_path, monkeypatch):
    proc = Child(["aprs"])
    decoder = _aprs(proc)
    entered, open_gate, start_sweep = (threading.Event() for _ in range(3))

    def diagnostic(stage):
        if stage is proc:
            entered.set()
            assert open_gate.wait(6)
            raise OSError("bad diagnostic stream")
        return []

    class Sweep(_StubSweeper):
        def start(self):
            super().start()
            start_sweep.set()

    monkeypatch.setattr(childproc, "stderr_tail", diagnostic)
    with _client(tmp_path, probe=_present, aprs_factory=lambda: decoder,
                 sweeper_factory=lambda: Sweep([])) as client:
        decoder.start()
        proc.code = 7
        first = threading.Thread(target=lambda: client.get("/api/aprs"), daemon=True)
        second = threading.Thread(target=lambda: client.get("/api/spectrum"), daemon=True)
        first.start()
        try:
            assert entered.wait(5)
            second.start()
            assert start_sweep.wait(1), "diagnostics held the next claim"
            assert proc.poll() == 7
        finally:
            open_gate.set()
            first.join(6)
            if second.ident is not None:
                second.join(6)
        assert client.app.state.sweeper.running


def test_shutdown_interpreter_exits_at_deadline_with_a_stalled_worker(tmp_path):
    code = """
import asyncio
import threading
from pathlib import Path
import sys
from sigint101 import app
from tests.test_app import _client

app.SHUTDOWN_TOTAL_S = 0.4
class Flock:
    def stop(self):
        print('worker parked', flush=True)
        threading.Event().wait()

async def run():
    console = _client(Path(sys.argv[1]), flock_factory=Flock).app
    async with console.router.lifespan_context(console):
        pass

asyncio.run(run())
print('lifespan exited', flush=True)
"""
    with subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        try:
            assert select.select([proc.stdout], [], [], 8)[0], "shutdown never reached the worker"
            assert proc.stdout.readline() == b"worker parked\n"
            out, err = proc.communicate(timeout=1.4)
            assert proc.returncode == 0, err.decode()
            assert b"lifespan exited" in out
        finally:
            if proc.poll() is None:
                proc.kill()


def test_ppm_retains_its_long_capture_window_without_losing_typed_queue_refusals(
        tmp_path, monkeypatch):
    entered, released, measuring, capture_done = (threading.Event() for _ in range(4))
    monkeypatch.setattr(app, "CLAIM_QUEUE_WAIT_S", 0.1, raising=False)

    class Sweep(_StubSweeper):
        def start(self):
            entered.set()
            assert released.wait(6)
            super().start()

    def measure(**kw):
        measuring.set()
        assert capture_done.wait(8)
        return {"hz": kw["hz"], "ppm": 1.0, "reason": ""}

    with _client(tmp_path, probe=_present, ppm_measure=measure,
                 sweeper_factory=lambda: Sweep([])) as client:
        claims = {}

        def request(method, url, done):
            claims[url] = client.request(method, url)
            done.set()

        finished = threading.Event()
        first = threading.Thread(target=request, daemon=True,
                                 args=("GET", "/api/spectrum", threading.Event()))
        first.start()
        assert entered.wait(2)
        refusal = threading.Thread(target=request, daemon=True,
                                   args=("POST", "/api/ppm/measure", finished))
        refusal.start()
        try:
            assert finished.wait(1), "an ordinary reservation never gave the queued ppm its refusal"
            assert claims["/api/ppm/measure"].status_code == 503
            assert "busy" in claims["/api/ppm/measure"].json()["reason"]
        finally:
            released.set()
            first.join(6)
            capture_done.set()
            refusal.join(6)

        capture_done.clear()
        ppm = threading.Thread(target=request, daemon=True,
                               args=("POST", "/api/ppm/measure", threading.Event()))
        ppm.start()
        assert measuring.wait(2)
        finished.clear()
        waterfall = threading.Thread(target=request, daemon=True,
                                     args=("GET", "/api/spectrum", finished))
        waterfall.start()
        try:
            assert not finished.wait(3), "the waterfall refused while ppm owned the radio"
        finally:
            capture_done.set()
            ppm.join(6)
            waterfall.join(6)
        assert finished.is_set()
        assert claims["/api/spectrum"].status_code == 200
