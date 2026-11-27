# ABOUTME: Exercises SDR reservations and bounded shutdown through the console.
# ABOUTME: Uses process and logging gates instead of requiring a radio.

import asyncio
import io
import select
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from sigint101 import app, aprs, childproc, radio_control, receiver_lifecycle
from sigint101.events import read_events
from tests.child_fakes import Child, LateProc
from tests.test_app import _client, _present, _StubSweeper
from tests.test_apt_retirement import _decoder as apt_decoder
from tests.test_stale_readers import APRS_1


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


def test_failed_reap_retries_with_kill_and_allows_handoff_after_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.05)
    can_reap, stop_polls, polled, retried = (threading.Event() for _ in range(4))
    claims, launches = [], []

    class RecoveringChild(Child):
        def kill(self):
            self.unkillable = not can_reap.is_set()
            super().kill()
            if polled.is_set() and len(self.sent) > 2:
                retried.set()

    proc = RecoveringChild(["aprs"], unkillable=True)
    decoder = _aprs(proc)

    class Sweep(_StubSweeper):
        def start(self):
            launches.append(proc.poll())
            super().start()

    with _client(tmp_path, probe=_present, aprs_factory=lambda: decoder,
                 sweeper_factory=lambda: Sweep([])) as client:
        decoder.start()
        ticket = decoder._run

        def poll_for_sweep():
            while not stop_polls.is_set():
                claims.append(client.get("/api/spectrum").status_code)
                if len(claims) >= 2:
                    polled.set()
                stop_polls.wait(0.01)

        poller = threading.Thread(target=poll_for_sweep, daemon=True)
        poller.start()
        try:
            assert retried.wait(3), "no background retry ran during refused claims"
            assert ticket.physical.is_set() and not ticket.reaped
            with decoder._runs:
                assert not decoder._runs.is_current(ticket.number)
            assert proc.poll() is None and launches == []
        finally:
            stop_polls.set()
            poller.join(3)
            can_reap.set()

        assert not poller.is_alive()
        assert claims and set(claims) == {503}
        assert ticket.complete.wait(3), "the killable child was never reaped"
        assert ticket.reaped and proc.poll() == -signal.SIGKILL
        assert proc.sent[0] == "TERM"
        assert len(proc.sent) >= 4 and set(proc.sent[1:]) == {"KILL"}
        assert client.get("/api/spectrum").status_code == 200
        assert launches == [-signal.SIGKILL]


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


def test_arbiter_release_can_finish_while_the_same_run_retires(tmp_path, monkeypatch):
    proc = Child(["aprs"])
    decoder = _aprs(proc)
    entered, resume, swept = (threading.Event() for _ in range(3))

    def tail(_stage):
        entered.set()
        assert resume.wait(7)
        return []

    def start_again():
        try:
            decoder.start()
        except aprs.AprsUnavailable:
            pass

    class Sweep(_StubSweeper):
        def start(self):
            super().start()
            swept.set()

    monkeypatch.setattr(childproc, "stderr_tail", tail)
    with _client(tmp_path, probe=_present, aprs_factory=lambda: decoder,
                 sweeper_factory=lambda: Sweep([])) as client:
        decoder.start()
        proc.code = 7
        old = threading.Thread(target=start_again, daemon=True)
        successor = threading.Thread(target=lambda: client.get("/api/spectrum"), daemon=True)
        old.start()
        try:
            assert entered.wait(2)
            successor.start()
            assert swept.wait(5), "an off-lock diagnostic still owned the radio"
            assert decoder.released
        finally:
            resume.set()
            old.join(6)
            if successor.ident is not None:
                successor.join(6)
        assert not decoder.running


def test_handoff_waits_for_reap_but_not_for_diagnostics_or_a_reader_hook(tmp_path):
    entered, hooked, stopping, reaped, swept = (threading.Event() for _ in range(5))

    class Proc(LateProc):
        def terminate(self):
            stopping.set()

        def wait(self, timeout=None):
            if not reaped.wait(timeout):
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.returncode = -15
            return self.returncode

    class Sweep(_StubSweeper):
        def start(self):
            super().start()
            swept.set()

    proc = Proc(["aprs", "--run-one"])
    decoder = _aprs(proc)
    with _client(tmp_path, probe=_present, aprs_factory=lambda: decoder,
                 sweeper_factory=lambda: Sweep([])) as client:
        heard = decoder.on_reception

        def park(item, *, command=None, freq_hz=None, radio=None):
            entered.set()
            assert hooked.wait(6)
            heard(item, command=command, freq_hz=freq_hz, radio=radio)

        decoder.on_reception = park
        decoder.start()
        runner = threading.Thread(target=lambda: client.get("/api/spectrum"), daemon=True)
        try:
            proc.feed(APRS_1)
            assert entered.wait(2)
            runner.start()
            assert stopping.wait(2)
            assert proc.poll() is None and not swept.is_set()
            reaped.set()
            assert swept.wait(1), "the old hook delayed handoff after reap"
            assert proc.poll() == -15
        finally:
            reaped.set()
            hooked.set()
            proc.close_pipe()
            if runner.ident is not None:
                runner.join(6)
            proc.reader.join(6)
        [record] = [row["payload"] for row in read_events(tmp_path / "events.jsonl")
                    if row["kind"] == "reception"]
        assert record["command"] == "aprs --run-one"
        assert record["freq_hz"] == aprs.DEFAULT_APRS_HZ
        assert record["radio"]["kind"] == "rtl-sdr"


def test_shutdown_cancels_grace_and_releases_other_radios_while_reporting_is_blocked(tmp_path):
    decoder, passes = apt_decoder(tmp_path)
    entered, resume, flock_stopped = (threading.Event() for _ in range(3))

    class Flock:
        def stop(self):
            flock_stopped.set()

    def hold(_item):
        entered.set()
        assert resume.wait(6)

    console = _client(tmp_path, apt_factory=lambda: decoder, flock_factory=Flock).app

    async def lifespan():
        async with console.router.lifespan_context(console):
            decoder.on_reception = hold
            decoder.start()
            proc, output = passes[0]
            (output / "last.png").touch()
            proc.code = 0

    worker = threading.Thread(target=lambda: asyncio.run(lifespan()), daemon=True)
    worker.start()
    try:
        assert entered.wait(2)
        assert flock_stopped.wait(1), "independent teardown waited for an APT hook"
        assert worker.is_alive()
    finally:
        resume.set()
        worker.join(6)
    assert not worker.is_alive()
    assert console.state.shutdown_errors == []


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


def test_receiver_shutdown_times_out_waiting_for_a_reaped_run_to_report(monkeypatch):
    monkeypatch.setattr(receiver_lifecycle, "SHUTDOWN_REPORT_WAIT_S", 0.05, raising=False)
    proc = Child(["aprs"])
    decoder = _aprs(proc)
    entered, resume, stopped = (threading.Event() for _ in range(3))
    errors = []

    def hold_report(_run):
        entered.set()
        resume.wait(5)

    def shutdown():
        try:
            decoder.shutdown()
        except aprs.AprsUnavailable as exc:
            errors.append(str(exc))
        finally:
            stopped.set()

    monkeypatch.setattr(decoder, "_report", hold_report)
    decoder.start()
    ticket = decoder._run
    worker = threading.Thread(target=shutdown, daemon=True)
    worker.start()
    try:
        assert entered.wait(2)
        assert ticket.reaped and proc.poll() is not None
        assert stopped.wait(1), "shutdown still waited for a stalled report"
        assert errors == ["rtl_fm shutdown reports still pending"]
        assert not ticket.complete.is_set()
    finally:
        resume.set()
        worker.join(2)
        assert ticket.complete.wait(2)


def test_retirement_bounds_diagnostic_wait_and_closes_stderr_when_safe(monkeypatch):
    monkeypatch.setattr(receiver_lifecycle, "DIAGNOSTIC_WAIT_S", 0.05, raising=False)
    entered, resume, closed = (threading.Event() for _ in range(3))

    class StalledStderr(io.StringIO):
        def read(self, *args):
            entered.set()
            resume.wait(5)
            return super().read(*args)

        def close(self):
            super().close()
            closed.set()

    proc = Child(["aprs"], code=1)
    proc.stderr.close()
    proc.stderr = StalledStderr("exit detail")
    decoder = _aprs(proc)
    decoder.start()
    ticket = decoder._run
    try:
        assert entered.wait(2)
        assert ticket.complete.wait(1), "retirement still waited for stalled stderr"
        assert ticket.reaped and not proc.stderr.closed
    finally:
        resume.set()
        assert closed.wait(2)
    assert ticket.diagnosed.is_set()
    assert "exit detail" in decoder.watch.reason


@pytest.mark.parametrize("transport", [False, True], ids=["pipe", "feed"])
@pytest.mark.parametrize("failure", ["stop", "join"])
def test_cleanup_fault_still_fences_the_run_and_closes_its_transport(
        monkeypatch, transport, failure):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.02)
    events = []
    fault = threading.Event()
    thread_errors = []

    def record(action):
        with decoder._runs:
            events.append((action, decoder._runs.is_current(ticket.number)))

    class Proc(Child):
        def terminate(self):
            record("terminate")
            super().terminate()

        def wait(self, timeout=None):
            if failure == "stop":
                raise OSError("child wait failed")
            return super().wait(timeout)

    class Feed(Proc):
        def stop_child(self):
            return childproc.stop_process(self)

        def close_transport(self):
            record("close")

    class Reader:
        def join(self, timeout=None):
            record("join")
            raise RuntimeError("reader join failed")

    def capture_thread_error(args):
        thread_errors.append(args.exc_value)
        fault.set()

    monkeypatch.setattr(threading, "excepthook", capture_thread_error)
    proc = (Feed if transport else Proc)(["aprs"])
    decoder = _aprs(proc)
    decoder.start()
    ticket = decoder._run
    decoder._thread = Reader()
    proc.code = 1
    ticket.wake.set()
    assert ticket.complete.wait(2)
    with decoder._runs:
        assert not decoder._runs.is_current(ticket.number)
    assert proc.poll() == 1 and ticket.reaped
    assert events[0] == ("terminate", True)
    assert proc.sent == ["TERM"]
    if failure == "stop":
        assert ticket.reap_failed and "child wait failed" in ticket.failure
        assert all(action != "join" for action, _ in events)
    else:
        assert not ticket.reap_failed
        assert fault.wait(2)
        assert [str(exc) for exc in thread_errors] == ["reader join failed"]
        assert events[1] == ("join", True)
    close_states = [current for action, current in events if action == "close"]
    assert bool(close_states) == transport
    assert not any(close_states)


@pytest.mark.parametrize("switched_at, ordinary_wait", [(0.5, [1.5]), (5, [])])
def test_queued_claim_uses_the_current_holder_bound_after_ppm(
        monkeypatch, switched_at, ordinary_wait):
    arbiter = radio_control.RadioArbiter()
    now = 0
    waits = []
    monkeypatch.setattr(radio_control, "time", SimpleNamespace(monotonic=lambda: now))

    def wait(left):
        nonlocal now
        waits.append(left)
        if len(waits) == 1:
            now = switched_at
            arbiter._claim = ("aprs", now)
        else:
            now += left

    monkeypatch.setattr(arbiter._lock, "wait", wait)
    with arbiter.reserve("ppm", AssertionError, float("inf")):
        with pytest.raises(RuntimeError, match="radio is busy"):
            with arbiter.reserve("spectrum", RuntimeError, radio_control.CLAIM_QUEUE_WAIT_S):
                pytest.fail("a second caller acquired the radio")
    assert waits == [radio_control.PPM_HOLD_S, *ordinary_wait]


def _queue_behind(monkeypatch, holder, started, takeover):
    """Queue a spectrum claim at t=0 behind `holder`; `takeover(now, left)` drives each wait."""
    arbiter = radio_control.RadioArbiter()
    clock = SimpleNamespace(now=started, waits=[], granted=None, refused_at=None)
    monkeypatch.setattr(radio_control, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def wait(left):
        assert left > 0, "the queued claim waited for no time (busy-wait)"
        assert len(clock.waits) < 50, "the queued claim kept waiting without a ceiling"
        clock.waits.append(left)
        claim = takeover(clock.now, left)
        if claim is False:
            clock.now += left
        else:
            clock.now, arbiter._claim = claim

    monkeypatch.setattr(arbiter._lock, "wait", wait)
    with arbiter.reserve(holder, AssertionError, float("inf")):
        clock.now = 0
        try:
            with arbiter.reserve("spectrum", RuntimeError, radio_control.CLAIM_QUEUE_WAIT_S):
                clock.granted = arbiter._claim
        except RuntimeError as exc:
            assert str(exc) == "the radio is busy finishing another request"
            clock.refused_at = clock.now
    return clock


def test_queued_claim_gives_up_at_its_own_ppm_ceiling_when_a_second_ppm_takes_over(monkeypatch):
    def takeover(now, left):
        # ppm claim B wins the wake-up at t=12; after that every wait runs out.
        return (12, ("ppm", 12)) if now == 0 else False

    clock = _queue_behind(monkeypatch, "ppm", 0, takeover)
    assert clock.granted is None
    assert clock.refused_at == pytest.approx(radio_control.PPM_HOLD_S)
    assert clock.waits == [radio_control.PPM_HOLD_S, radio_control.PPM_HOLD_S - 12]


@pytest.mark.parametrize("period", [1, 5, 12, 19.5])
def test_a_stream_of_ppm_claims_cannot_hold_a_queued_claim_past_a_fixed_ceiling(
        monkeypatch, period):
    def takeover(now, left):
        # A fresh ppm claim takes the radio every `period` seconds for as long as anyone waits.
        if left <= period:
            return False
        return (now + period, ("ppm", now + period))

    clock = _queue_behind(monkeypatch, "ppm", 0, takeover)
    assert clock.granted is None
    assert clock.refused_at == pytest.approx(radio_control.PPM_HOLD_S)


def test_a_single_ppm_holder_keeps_its_own_bound_and_hands_over_on_release(monkeypatch):
    # ppm started 5 s before the claim queued: its own bound ends first, at t=15.
    clock = _queue_behind(monkeypatch, "ppm", -5, lambda now, left: False)
    assert clock.waits == [radio_control.PPM_HOLD_S - 5]
    assert clock.refused_at == pytest.approx(radio_control.PPM_HOLD_S - 5)

    # A second ppm claim that releases inside the ceiling hands the radio to the queued claim.
    def takeover(now, left):
        return {0: (12, ("ppm", 12)), 12: (18, None)}[now]

    clock = _queue_behind(monkeypatch, "ppm", 0, takeover)
    assert clock.refused_at is None
    assert clock.granted == ("spectrum", 18)


def test_a_queued_claim_behind_an_ordinary_holder_keeps_the_ordinary_deadline(monkeypatch):
    clock = _queue_behind(monkeypatch, "aprs", 0, lambda now, left: False)
    assert clock.waits == [radio_control.CLAIM_QUEUE_WAIT_S]
    assert clock.refused_at == pytest.approx(radio_control.CLAIM_QUEUE_WAIT_S)


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
