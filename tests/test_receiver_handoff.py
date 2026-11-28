# ABOUTME: Exercises SDR reservations and bounded shutdown through the console.
# ABOUTME: Uses process and logging gates instead of requiring a radio.

import asyncio
import io
import select
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from types import SimpleNamespace

import pytest

from sigint101 import adsb, app, aprs, childproc, decode, ppm, radio_control, receiver_lifecycle
from sigint101.events import read_events
from sigint101.spectrum import Sweeper
from tests.child_fakes import Child, LateProc
from tests.test_app import _client, _present, _rows, _StubSweeper
from tests.test_apt_retirement import _decoder as apt_decoder
from tests.test_spectrum import FedProc, SignalledProc, SlowStopProc
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


def test_an_rtl_433_that_survived_kill_holds_the_radio_until_it_is_reaped(tmp_path):
    # Codex D-091 F7: rtl_433's decoder told one refusal and then let a claim spawn beside the
    # live child. It now answers the handoff check like the supervised receivers do.
    rtl_433 = Child(["rtl_433"], unkillable=True)
    sensors = decode.Decoder(spawn=lambda *_: rtl_433, radio_present=lambda: True,
                             which=lambda t: t)
    aprs_children = []

    def spawn_aprs(*_):
        aprs_children.append(Child(["rtl_fm"]))
        return aprs_children[-1]

    with _client(tmp_path, probe=_present, decoder_factory=lambda: sensors,
                 aprs_factory=lambda: aprs.AprsDecoder(spawn=spawn_aprs,
                                                       radio_present=lambda: True,
                                                       which=lambda t: t)) as client:
        assert client.get("/api/decode").status_code == 200
        refused = [client.get("/api/aprs") for _ in range(3)]   # the stand-down cannot kill it
        refused += [client.get("/api/decode") for _ in range(2)]
        assert not aprs_children                               # nothing spawned beside it
        rtl_433.code = -signal.SIGKILL                         # the kill lands at last
        back = client.get("/api/aprs")
        client.app.state.aprs.stop()
    for resp in refused:
        assert resp.status_code == 503
        assert "rtl_433 survived kill; the dongle may still be held" in resp.json()["reason"]
    assert back.status_code == 200 and len(aprs_children) == 1


def _sensors(spawn):
    return decode.Decoder(spawn=spawn, radio_present=lambda: True, which=lambda t: t)


def test_a_legacy_retune_hands_the_radio_on_without_waiting_on_an_exit_diagnostic(
        tmp_path, monkeypatch):
    # Codex D-093 F1: the retune read a status-1 exit's stderr under the decoder's life lock,
    # so a stalled diagnostic held the stand-down that a spectrum claim needs.
    entered, resume, swept = (threading.Event() for _ in range(3))
    procs = []

    def spawn(*_):
        procs.append(LateProc(["rtl_433"]))
        return procs[-1]

    def stalled_tail(_stage):
        entered.set()
        resume.wait(10)
        return []

    class Sweep(_StubSweeper):
        def start(self):
            super().start()
            swept.set()

    sensors = _sensors(spawn)
    sensors.STOP_WAIT_S = 0.05
    monkeypatch.setattr(childproc, "stderr_tail", stalled_tail)
    with _client(tmp_path, probe=_present, decoder_factory=lambda: sensors,
                 sweeper_factory=lambda: Sweep([])) as client:
        assert client.get("/api/decode").status_code == 200
        procs[0].die(1)                         # status 1; its pipe is still open
        retune = threading.Thread(target=sensors.retune, args=(315_000_000,), daemon=True)
        successor = threading.Thread(target=lambda: client.get("/api/spectrum"), daemon=True)
        try:
            retune.start()                      # first, so its hold on the decoder is what's tested
            retune.join(3)
            assert not retune.is_alive(), "the retune waited on the exit diagnostic"
            successor.start()
            assert swept.wait(5), "a stalled exit diagnostic still held the radio"
            assert not entered.is_set()
        finally:
            resume.set()
            retune.join(6)
            if successor.ident is not None:
                successor.join(6)
            for proc in procs:
                proc.close_pipe()


class _WaitFails(Child):
    def __init__(self):
        super().__init__(["rtl_433"], unkillable=True)   # survives the escalated SIGKILL too

    def wait(self, timeout=None):
        if self.code is None and "TERM" in self.sent:   # the stop's wait, not the reader's
            raise OSError("wait failed")
        return super().wait(timeout)


def test_an_rtl_433_whose_reap_wait_fails_holds_the_radio_until_its_exit_shows(tmp_path):
    # Codex D-093 F2: an OSError from the stop's wait dropped the live child, and the next
    # spectrum claims were granted beside it.
    rtl_433 = _WaitFails()
    with _client(tmp_path, probe=_present, decoder_factory=lambda: _sensors(lambda *_: rtl_433),
                 sweeper_factory=lambda: _StubSweeper([])) as client:
        assert client.get("/api/decode").status_code == 200
        refused = [client.get("/api/spectrum") for _ in range(3)]
        assert not client.app.state.sweeper.running
        hardware = client.get("/api/hardware").json()["state"]
        rtl_433.code = -signal.SIGKILL                        # its exit is confirmed at last
        back = client.get("/api/spectrum")
    for resp in refused:
        assert resp.status_code == 503
        reason = resp.json()["reason"]
        assert "rtl_433 could not be confirmed stopped (wait failed)" in reason
        assert "the dongle may still be held" in reason
    assert hardware == "busy"
    assert back.status_code == 200


class _SlowToStop(Child):
    def __init__(self):
        super().__init__(["rtl_433"])
        self.waiting, self.gate = threading.Event(), threading.Event()

    def terminate(self):
        self.sent.append("TERM")

    def wait(self, timeout=None):
        if self.code is None and "TERM" in self.sent:   # the stop's reap, not the reader's
            self.waiting.set()
            if not self.gate.wait(5):
                raise subprocess.TimeoutExpired("rtl_433", timeout)
            self.code = -signal.SIGTERM
        return super().wait(timeout)


def test_hardware_reads_busy_while_a_legacy_stop_waits_on_its_reap(tmp_path):
    # Codex D-093 F3: in flight, the child was in neither ownership field, so the status bar
    # read "present" while rtl_433 still held the dongle.
    rtl_433 = _SlowToStop()
    sensors = _sensors(lambda *_: rtl_433)
    with _client(tmp_path, probe=_present, decoder_factory=lambda: sensors) as client:
        assert client.get("/api/decode").status_code == 200
        retune = threading.Thread(target=sensors.retune, args=(315_000_000,), daemon=True)
        retune.start()
        try:
            assert rtl_433.waiting.wait(5)
            during = client.get("/api/hardware").json()["state"]
        finally:
            rtl_433.gate.set()
            retune.join(5)
        after = client.get("/api/hardware").json()["state"]
    assert during == "busy"
    assert after == "present"


def _sweep(first):
    """A real Sweeper whose first rtl_power is `first()`; every later start gets a FedProc."""
    procs = []

    def spawn(_cmd):
        procs.append(first() if not procs else FedProc())
        return procs[-1]

    sweeper = Sweeper(spawn=spawn, radio_present=lambda: True)
    sweeper.STOP_WAIT_S = 0.05
    return sweeper, procs


@pytest.mark.parametrize("held,reason", [
    (lambda: SignalledProc(unkillable=True),
     "rtl_power survived kill; the dongle may still be held"),
    (lambda: SignalledProc(unkillable=True, wait_error="cannot read the exit status"),
     "rtl_power could not be confirmed stopped (cannot read the exit status); "
     "the dongle may still be held"),
], ids=["survived-kill", "wait-error"])
def test_an_rtl_power_the_stop_cannot_confirm_holds_the_radio_until_it_is_reaped(
        tmp_path, held, reason):
    # C77 (C73 build review F1): the sweeper never answered the handoff check, so ppm and the
    # decoders opened the radio beside an rtl_power that outlived its stop, and the status bar read
    # present. Its next start reported the failure once, and the one after spawned a second sweep.
    sweeper, sweeps = _sweep(held)
    measured, aprs_children = [], []

    def measure(**kwargs):
        measured.append(kwargs)
        return {"hz": kwargs["hz"], "ppm": None, "reason": "no pilot to measure against"}

    def spawn_aprs(*_):
        aprs_children.append(Child(["rtl_fm"]))
        return aprs_children[-1]

    receiver = aprs.AprsDecoder(spawn=spawn_aprs, radio_present=lambda: True, which=lambda t: t)
    with _client(tmp_path, probe=_present, ppm_measure=measure, sweeper_factory=lambda: sweeper,
                 aprs_factory=lambda: receiver) as client:
        assert client.get("/api/spectrum").status_code == 200
        refused = [client.post("/api/ppm/measure"), client.get("/api/aprs"),
                   client.post("/api/ppm/measure"), client.get("/api/aprs"),
                   client.get("/api/spectrum"), client.get("/api/spectrum")]
        hardware = client.get("/api/hardware").json()["state"]
        assert not measured and not aprs_children        # nothing opened the radio beside it
        assert len(sweeps) == 1 and not sweeper.running
        assert sweeps[0].sent == ["TERM", "KILL"]
        sweeps[0].die(-signal.SIGKILL)                    # its exit is confirmed at last
        after = client.get("/api/hardware").json()["state"]
        back = [client.post("/api/ppm/measure"), client.get("/api/aprs")]
        client.app.state.aprs.stop()
    for resp in refused:
        assert resp.status_code == 503
        assert resp.json()["reason"] == reason
    assert hardware == "busy"
    assert after == "present"
    assert [resp.status_code for resp in back] == [200, 200]
    assert len(measured) == 1 and len(aprs_children) == 1


def test_hardware_reads_busy_while_a_sweep_stop_waits_on_its_reap(tmp_path):
    # C77: in flight, the stopping rtl_power was in no ownership field, so the bar read present.
    sweeper, sweeps = _sweep(SlowStopProc)
    with _client(tmp_path, probe=_present, sweeper_factory=lambda: sweeper) as client:
        assert client.get("/api/spectrum").status_code == 200
        retune = threading.Thread(target=sweeper.retune, args=(88_000_000, 93_000_000, 78_125),
                                  daemon=True)
        retune.start()
        try:
            assert sweeps[0].waiting.wait(5)
            during = client.get("/api/hardware").json()["state"]
        finally:
            sweeps[0].gate.set()
            retune.join(5)
        after = client.get("/api/hardware").json()["state"]
    assert during == "busy"
    assert after == "present"


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


@pytest.mark.parametrize("method,url,queued", [
    ("GET", "/api/spectrum", True),
    ("GET", "/api/spectrum", False),
    ("GET", "/api/provenance", False),
    ("POST", "/api/provenance/baseline", False),
], ids=["queued-spectrum", "later-spectrum", "provenance", "baseline"])
def test_unreaped_ppm_capture_holds_the_radio_until_exit_is_confirmed(
        tmp_path, monkeypatch, method, url, queued):
    proc = Child(["rtl_fm"], unkillable=True)
    measuring, finish, waiting = (threading.Event() for _ in range(3))
    launches = []

    def capture_blocks(stream, **_):
        measuring.set()
        assert finish.wait(5)
        stream.stalled = True
        return [], None

    class Sweep(_StubSweeper):
        def start(self):
            launches.append(proc.poll())
            super().start()

    monkeypatch.setattr(ppm, "spawn", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(ppm, "block_phasors", capture_blocks)
    with (_client(tmp_path, probe=_present, sweeper_factory=lambda: Sweep(_rows())) as client,
          ThreadPoolExecutor(max_workers=2) as requests):
        condition = client.app.state.radio_arbiter._lock
        real_wait = condition.wait

        def visible_wait(timeout):
            waiting.set()
            return real_wait(timeout)

        monkeypatch.setattr(condition, "wait", visible_wait)
        calibration = requests.submit(client.post, "/api/ppm/measure")
        try:
            assert measuring.wait(5)
            if queued:
                waiter = requests.submit(client.request, method, url)
                assert waiting.wait(5)
        finally:
            finish.set()

        measured = calibration.result(timeout=5)
        if queued:
            refused = waiter.result(timeout=5)
        else:
            refused = client.request(method, url)
        assert measured.status_code == 200 and measured.json()["ppm"] is None
        assert "may still hold the radio" in measured.json()["reason"]
        assert proc.sent == ["TERM", "KILL"] and proc.poll() is None
        if url == "/api/spectrum":
            assert refused.status_code == 503
            assert "calibration" in refused.json()["reason"]
            assert "may still hold the radio" in refused.json()["reason"]
        elif method == "GET":
            assert refused.status_code == 200 and refused.json()["provenance"]["ref_db"] is None
        else:
            assert refused.status_code == 400 and refused.json()["recorded"] is False
        assert not launches
        assert client.get("/api/hardware").json()["state"] == "busy"
        assert client.post("/api/ppm/measure").status_code == 503
        assert proc.sent == ["TERM", "KILL"]

        proc.code = -signal.SIGKILL
        assert client.get("/api/hardware").json()["state"] == "present"
        assert client.request(method, url).status_code == 200
        assert launches == [-signal.SIGKILL]


@pytest.mark.parametrize("unkillable", [False, True], ids=["killed", "unconfirmed"])
def test_ppm_wait_error_escalates_and_holds_the_radio_until_exit(tmp_path, monkeypatch, unkillable):
    class WaitError(Child):
        def wait(self, timeout=None):
            raise OSError("cannot read the exit status")

    proc = WaitError(["rtl_fm"], stubborn=True, unkillable=unkillable)
    monkeypatch.setattr(ppm, "spawn", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(ppm, "block_phasors", lambda *_args, **_kwargs: ([], None))
    with _client(tmp_path, probe=_present, sweeper_factory=lambda: _StubSweeper([])) as client:
        measured = client.post("/api/ppm/measure")
        assert measured.status_code == 200 and measured.json()["ppm"] is None
        assert proc.sent == ["TERM", "KILL"]
        if unkillable:
            assert "may still hold the radio" in measured.json()["reason"]
            assert client.get("/api/spectrum").status_code == 503
            assert not client.app.state.sweeper.running
            proc.code = -signal.SIGKILL
        else:
            assert proc.poll() == -signal.SIGKILL
            assert "ended before enough audio" in measured.json()["reason"]
        assert client.get("/api/spectrum").status_code == 200


def test_provenance_poll_leaves_a_released_sweeper_passive(tmp_path):
    # Guard the case where the sweep was already released before provenance checked it.
    proc = Child(["rtl_fm"])
    receiver = _aprs(proc)
    sweeper = Sweeper(radio_present=lambda: True,
                      spawn=lambda *_: pytest.fail("provenance took the radio from APRS"))
    with _client(tmp_path, probe=_present, aprs_factory=lambda: receiver,
                 sweeper_factory=lambda: sweeper) as client:
        assert client.get("/api/aprs").status_code == 200
        assert sweeper.released
        provenance = client.get("/api/provenance")
        assert provenance.status_code == 200 and provenance.json()["provenance"]["ref_db"] is None
        assert receiver.running and not proc.sent


def test_provenance_queued_behind_aprs_preserves_the_decoder_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CLAIM_QUEUE_WAIT_S", 10)
    checked, spawning, waiting, finish = (threading.Event() for _ in range(4))
    sweep_children = []
    proc = Child(["rtl_fm"])

    class Sweep(Sweeper):
        @property
        def released(self):
            released = super().released
            if not released:
                # Provenance has read the old state. Let APRS release the sweep before it queues.
                checked.set()
                assert spawning.wait(5)
            return released

    def spawn_aprs(*_):
        spawning.set()
        assert finish.wait(5)
        return proc

    def spawn_sweep(*_):
        child = Child(["rtl_power"])
        sweep_children.append(child)
        return child

    sweeper = Sweep(radio_present=lambda: True, spawn=spawn_sweep)
    receiver = aprs.AprsDecoder(spawn=spawn_aprs, radio_present=lambda: True, which=lambda t: t)
    with (_client(tmp_path, probe=_present, aprs_factory=lambda: receiver,
                  sweeper_factory=lambda: sweeper) as client,
          ThreadPoolExecutor(max_workers=2) as requests):
        condition = client.app.state.radio_arbiter._lock
        real_wait = condition.wait

        def visible_wait(timeout):
            waiting.set()
            return real_wait(timeout)

        monkeypatch.setattr(condition, "wait", visible_wait)
        provenance = requests.submit(client.get, "/api/provenance")
        try:
            assert checked.wait(5), "provenance did not read the sweep's release state"
            decoder = requests.submit(client.get, "/api/aprs")
            assert waiting.wait(5), "provenance did not queue behind APRS"
            assert sweeper.released
        finally:
            finish.set()

        assert decoder.result(timeout=5).status_code == 200
        reading = provenance.result(timeout=5)
        assert reading.status_code == 200 and reading.json()["provenance"]["ref_db"] is None
        assert proc.sent == [], "provenance stood APRS down"
        assert receiver.running
        assert not sweep_children


def test_provenance_lazy_start_checks_other_receivers_for_failed_reap(tmp_path):
    proc = Child(["rtl_fm"], unkillable=True)
    receiver = _aprs(proc)
    with _client(tmp_path, probe=_present, aprs_factory=lambda: receiver,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        receiver.start()
        receiver.release()
        try:
            assert receiver.release_failure() and proc.poll() is None
            response = client.get("/api/provenance")
            assert response.status_code == 200
            assert response.json()["provenance"]["ref_db"] is None
            assert not client.app.state.sweeper.running
        finally:
            proc.code = -signal.SIGKILL


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


def test_shutdown_reports_an_unconfirmed_ppm_child(tmp_path, monkeypatch, caplog):
    proc = Child(["rtl_fm"], unkillable=True)
    monkeypatch.setattr(ppm, "spawn", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(ppm, "block_phasors", lambda *_args, **_kwargs: ([], None))
    with _client(tmp_path, probe=_present, sweeper_factory=lambda: _StubSweeper([])) as client:
        assert client.post("/api/ppm/measure").status_code == 200
        assert proc.poll() is None

    reason = "ppm: the calibration rtl_fm may still hold the radio because its exit is unconfirmed"
    assert client.app.state.shutdown_errors == [reason]
    assert f"console shutdown: {reason}" in caplog.messages


def test_shutdown_retries_a_retained_ppm_child(tmp_path, monkeypatch):
    class SecondKillStops(Child):
        def kill(self):
            self.unkillable = "KILL" not in self.sent
            super().kill()

    proc = SecondKillStops(["rtl_fm"], stubborn=True)
    monkeypatch.setattr(ppm, "spawn", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(ppm, "block_phasors", lambda *_args, **_kwargs: ([], None))
    with _client(tmp_path, probe=_present, sweeper_factory=lambda: _StubSweeper([])) as client:
        measured = client.post("/api/ppm/measure")
        assert measured.status_code == 200
        assert "may still hold the radio" in measured.json()["reason"]
        assert client.get("/api/spectrum").status_code == 503
        assert proc.sent == ["TERM", "KILL"]

    assert proc.sent == ["TERM", "KILL", "KILL"]
    assert proc.poll() == -signal.SIGKILL
    assert client.app.state.shutdown_errors == []


def test_shutdown_interpreter_exits_at_deadline_with_a_stalled_worker(tmp_path):
    """Two receivers and a store never return; shutdown still keeps its one deadline and exits.

    The deadline is checked against the clock readings finish_console itself makes, which the
    child records, not against wall time in this process: child start-up, interpreter teardown
    and coverage writes on a loaded machine can take seconds and are not what this proves. The
    guard below only catches a hang.
    """
    import json

    code = """
import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from sigint101 import app, exam, radio_control
from tests.test_app import _client

app.SHUTDOWN_TOTAL_S = float(sys.argv[2])
GUARD_S = float(sys.argv[3])
parked = {name: threading.Event() for name in ("flock", "rayhunter", "exam")}
phase, reads, unreached = [None], [], []


def stall(name):
    def park(*_):
        parked[name].set()
        threading.Event().wait()
    return park


class Flock:
    stop = stall("flock")


class Rayhunter:
    stop = stall("rayhunter")


exam.ExamStore.close = stall("exam")


def monotonic():
    # Each later read waits until the workers of its phase are parked, so every wait is sure to
    # be on a stalled worker however late a loaded machine runs it; the time is read after that.
    if reads:
        names = ("flock", "rayhunter") if phase[0] == "drain" else ("exam",)
        unreached.extend(name for name in names if not parked[name].wait(GUARD_S))
    now = time.monotonic()
    reads.append((phase[0], now))
    return now


radio_control.time = SimpleNamespace(monotonic=monotonic)
seal = radio_control.ReceptionSink.seal


def sealed(sink):
    seal(sink)
    phase[0] = "close"


radio_control.ReceptionSink.seal = sealed
finish = app.finish_console


async def finish_console(*args, **kwargs):
    phase[0] = "drain"
    return await finish(*args, **kwargs)


app.finish_console = finish_console


async def run():
    console = _client(Path(sys.argv[1]), flock_factory=Flock, rayhunter_factory=Rayhunter).app
    async with console.router.lifespan_context(console):
        pass
    return console.state.shutdown_errors


errors = asyncio.run(run())
print(json.dumps({"reads": reads, "unreached": unreached, "errors": errors}), flush=True)
"""

    def gave_up_at(reads, phase, until):
        # Every read before the last came before ``until``; the last, which ended the wait, after.
        times = [now for name, now in reads if name == phase]
        return bool(times) and times[-1] >= until and all(now < until for now in times[:-1])

    total, guard = 0.4, 10
    with subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(total), str(guard)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        try:
            assert select.select([proc.stdout], [], [], 6 * guard)[0], "shutdown hung"
            try:
                out, err = proc.communicate(timeout=6 * guard)
            except subprocess.TimeoutExpired:
                pytest.fail("the interpreter did not exit with stalled workers")
        finally:
            if proc.poll() is None:
                proc.kill()
    assert proc.returncode == 0, err.decode()
    report = json.loads(out.decode().splitlines()[-1])
    assert report["unreached"] == [], "a stalled worker never started"
    (first_phase, start), *reads = report["reads"]
    assert first_phase == "drain"
    # One deadline from the first read, as finish_console sets it: the receivers get all but a
    # quarter of it (one second at most), and the stores close by that same deadline.
    deadline = start + total
    drain_end = deadline - min(1.0, total / 4)
    assert gave_up_at(reads, "drain", drain_end), reads
    assert gave_up_at(reads, "close", deadline), reads
    assert {"flock: shutdown still pending", "rayhunter: shutdown still pending",
            "exam: close still pending"} <= set(report["errors"])


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


def test_receiver_shutdown_gives_two_stalled_reports_one_shared_deadline(monkeypatch):
    """Two older runs are still reporting; shutdown gives up on both within one budget.

    A fake clock moves only while a report is waited on, so the budget is read from the clock
    shutdown itself reads, not from wall time. The first report stalls but finishes inside the
    budget, the second never does: with one shared deadline the second gets only what is left,
    while a deadline reset per report would give it a whole budget of its own.
    """
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(receiver_lifecycle, "time",
                        SimpleNamespace(monotonic=lambda: clock.now))
    budget = receiver_lifecycle.SHUTDOWN_REPORT_WAIT_S
    waits = []

    class Report:
        def __init__(self, name, done_after):
            self.name, self.done_after = name, done_after

        def wait(self, timeout):
            waits.append((self.name, clock.now, timeout))
            if self.done_after is not None and self.done_after <= timeout:
                clock.now += self.done_after
                return True
            clock.now += timeout
            return False

    late = SimpleNamespace(complete=Report("late", budget * 2 / 3))
    stuck = SimpleNamespace(complete=Report("stuck", None))
    decoder = _aprs(Child(["aprs"]))
    # A list, not the set, so the report that finishes is waited on first, every time.
    decoder._reports = [late, stuck]
    start = clock.now

    with pytest.raises(aprs.AprsUnavailable, match="rtl_fm shutdown reports still pending"):
        decoder.shutdown()

    assert [name for name, _, _ in waits] == ["late", "stuck"]
    assert waits[0] == ("late", start, pytest.approx(budget))
    # The second wait ends at the same deadline: it gets only what the first one left.
    _, began, timeout = waits[1]
    assert began == pytest.approx(start + budget * 2 / 3)
    assert began + timeout == pytest.approx(start + budget)
    assert clock.now == pytest.approx(start + budget)


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
        def stop_child(self, **options):
            return childproc.stop_and_confirm(self, **options)

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
        assert not ticket.reap_failed and decoder.release_failure(pending=True) is None
        assert "child wait failed" in ticket.diagnostic_fault
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


def _queue_behind(monkeypatch, holder, started, takeover, arrival=0):
    """Queue a spectrum claim at `arrival` behind `holder`; `takeover(now, left)` drives waits."""
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
        clock.now = arrival
        try:
            with arbiter.reserve("spectrum", RuntimeError,
                                 arrival + radio_control.CLAIM_QUEUE_WAIT_S):
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


def test_the_ppm_ceiling_counts_from_the_claims_arrival_not_the_first_ppm_start(monkeypatch):
    # ppm A started at t=0, the claim queued at t=5, ppm B took over at t=12.
    def takeover(now, left):
        return (12, ("ppm", 12)) if now == 5 else False

    clock = _queue_behind(monkeypatch, "ppm", 0, takeover, arrival=5)
    assert clock.granted is None
    assert clock.refused_at == pytest.approx(5 + radio_control.PPM_HOLD_S)


def test_a_nearly_finished_ppm_holder_still_leaves_the_ordinary_deadline(monkeypatch):
    # ppm's own bound ends 1 s after the claim queued; the claim still waits its 2 s.
    clock = _queue_behind(monkeypatch, "ppm", 1 - radio_control.PPM_HOLD_S,
                          lambda now, left: False)
    assert clock.waits == [radio_control.CLAIM_QUEUE_WAIT_S]
    assert clock.refused_at == pytest.approx(radio_control.CLAIM_QUEUE_WAIT_S)


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


@pytest.mark.parametrize("transport", [False, True], ids=["pipe", "feed"])
def test_supervised_stop_escalates_after_wait_error(monkeypatch, transport):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 30)
    waits = []

    class Proc(Child):
        def wait(self, timeout=None):
            waits.append(tuple(self.sent))
            if "KILL" not in self.sent:
                raise OSError("TERM wait failed")
            return super().wait(timeout)

    class Feed(adsb._Feed):
        def _lines(self):
            return iter(())

    proc = Proc(stubborn=True)
    decoder = _aprs(Feed(proc) if transport else proc)
    decoder.start()
    try:
        decoder.stop()
        assert proc.sent == ["TERM", "KILL"]
        assert waits == [("TERM",), ("TERM", "KILL")]
        assert decoder._run.reaped
    finally:
        proc.code = -9
        _finish_fault_fake(decoder)


def _finish_fault_fake(decoder):
    with suppress(aprs.AprsUnavailable):
        decoder.shutdown()


@pytest.mark.parametrize("outcome,detail", [
    ("timeout", "survived kill"),
    ("wait-error", "KILL wait failed"),
    ("signal-error", "KILL signal failed"),
    ("status-error", "status unreadable"),
    ("no-status", "no exit status"),
])
def test_supervised_retained_reason_follows_kill_wait_outcome(
        tmp_path, monkeypatch, outcome, detail):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 30)
    waits = []

    class Proc(Child):
        def poll(self):
            if outcome == "status-error" and waits and self.code is None:
                raise OSError("status unreadable")
            return super().poll()

        def kill(self):
            super().kill()
            if outcome == "signal-error" and self.code is None:
                raise PermissionError("KILL signal failed")

        def wait(self, timeout=None):
            waits.append(tuple(self.sent))
            if "KILL" not in self.sent:
                raise OSError("TERM wait failed")
            if outcome == "wait-error" and self.code is None:
                raise OSError("KILL wait failed")
            if outcome == "no-status" and self.code is None:
                return 0
            return super().wait(timeout)

    proc = Proc(unkillable=True)
    decoder = _aprs(proc)
    with _client(tmp_path, probe=_present, aprs_factory=lambda: decoder,
                 sweeper_factory=lambda: _StubSweeper([])) as client:
        decoder.start()
        try:
            decoder.stop()
            reason = decoder.release_failure(pending=True)
            assert reason and detail in reason
            assert ("survived kill" in reason) == (outcome == "timeout")
            assert ("could not be confirmed stopped" in reason) == (outcome != "timeout")
            if outcome == "signal-error":
                assert waits == [("TERM",)]
            assert client.get("/api/hardware").json()["state"] == "busy"
            for _ in range(2):
                response = client.get("/api/spectrum")
                assert response.status_code == 503 and response.json()["reason"] == reason
            assert not client.app.state.sweeper.running
        finally:
            proc.code = -9


@pytest.mark.parametrize("control", ["stop", "release", "retune", "shutdown"])
def test_supervised_control_retries_kept_child_immediately(monkeypatch, control):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 30)
    proc = Child(unkillable=True)
    decoder = _aprs(proc)
    decoder.start()
    try:
        decoder.stop()
        assert proc.sent == ["TERM", "KILL"]
        assert decoder.release_failure()
        decoder.retune(decoder.freq_hz)
        assert proc.sent == ["TERM", "KILL"]
        proc.unkillable = False
        if control == "retune":
            decoder.retune(145_000_000)
        else:
            getattr(decoder, control)()
        assert proc.sent == ["TERM", "KILL", "KILL"]
        assert decoder._run.reaped
        assert decoder.release_failure(pending=True) is None
    finally:
        proc.code = -9
        _finish_fault_fake(decoder)


def test_supervised_reader_wakes_do_not_postpone_pending_retry(monkeypatch):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.2)
    done, retried = threading.Event(), threading.Event()

    class Proc(Child):
        def kill(self):
            super().kill()
            if len(self.sent) > 2:
                retried.set()

    proc = Proc(unkillable=True)
    decoder = _aprs(proc)
    decoder.start()
    run = decoder._run

    def reader_wakes():
        while not done.wait(0.01):
            run.wake.set()

    reader = threading.Thread(target=reader_wakes, daemon=True)
    reader.start()
    try:
        decoder.stop()
        assert retried.wait(1.5), "reader wakes postponed the periodic retry"
    finally:
        done.set()
        reader.join(2)
        proc.code = -9
        _finish_fault_fake(decoder)


def test_console_shutdown_reports_each_unconfirmed_radio_consumer_before_stores_close(
        monkeypatch, caplog):
    monkeypatch.setattr(app, "SHUTDOWN_TOTAL_S", 0.3)
    unblock = threading.Event()
    reasons = {
        "sweeper": "unconfirmed sweep",
        "aprs": "unconfirmed decoder",
        "ppm": "unconfirmed calibration",
    }
    logged_at_close = []

    class Consumer:
        def __init__(self, name):
            self.name = name

        def stop(self):
            if self.name == "sweeper":
                raise RuntimeError(reasons[self.name])
            if self.name == "aprs":
                unblock.wait(2)

        def release_failure(self, *, pending=False):
            assert pending == (self.name != "ppm")
            return reasons.get(self.name)

    def close():
        logged_at_close.append(tuple(caplog.messages))

    state = SimpleNamespace(**dict.fromkeys((*app.SDR_CONSUMERS, "lora", "flock", "rayhunter")))
    state.sweeper, state.aprs, state.ais = map(Consumer, ("sweeper", "aprs", "ais"))
    state.ppm_calibration = Consumer("ppm")
    state.radio_arbiter = radio_control.RadioArbiter()
    state.reception_sink = radio_control.ReceptionSink()
    state.progress = state.exam_store = SimpleNamespace(close=close)
    try:
        errors = asyncio.run(app.shutdown_console_state(state))
    finally:
        unblock.set()

    assert len(logged_at_close) == 2
    for name, reason in reasons.items():
        entry = f"{name}: {reason}"
        assert all(f"console shutdown: {entry}" in messages for messages in logged_at_close)
        assert caplog.messages.count(f"console shutdown: {entry}") == errors.count(entry) == 1
    assert set(errors) == {f"{name}: {reason}" for name, reason in reasons.items()} | {
        "aprs: shutdown still pending",
    }


def test_reader_drain_failure_keeps_a_confirmed_reap(monkeypatch):
    proc = Child()
    decoder = _aprs(proc)
    decoder.start()
    run = decoder._run

    def fail_drain(_run):
        raise OSError("could not join reader")

    monkeypatch.setattr(decoder, "_drain_reader", fail_drain)
    proc.code = 1
    run.wake.set()
    assert run.complete.wait(4)
    assert run.reaped and not run.reap_failed
    assert decoder.release_failure(pending=True) is None
    assert run.diagnostic_fault == "reader drain failed: could not join reader"


def test_supervised_controls_share_an_inflight_retry_acknowledgement(monkeypatch):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 30)
    retrying, can_reap, both_waiting = (threading.Event() for _ in range(3))

    class Proc(Child):
        def kill(self):
            if "KILL" in self.sent:
                retrying.set()
                self.unkillable = not can_reap.wait(3)
            super().kill()

    proc = Proc(unkillable=True)
    decoder = _aprs(proc)
    decoder.start()
    decoder.stop()
    wait = decoder._wait_physical
    waits = []

    def observe_wait(*args):
        waits.append(args)
        if len(waits) == 2:
            both_waiting.set()
        return wait(*args)

    monkeypatch.setattr(decoder, "_wait_physical", observe_wait)
    try:
        with ThreadPoolExecutor(max_workers=2) as controls:
            try:
                stop = controls.submit(decoder.stop)
                assert retrying.wait(2), "the stop did not request an immediate retry"
                release = controls.submit(decoder.release)
                assert both_waiting.wait(2)
                assert waits[0][2] is not None and waits[0][2] is waits[1][2]
                assert not stop.done() and not release.done()
            finally:
                can_reap.set()
        stop.result()
        release.result()
        assert proc.sent == ["TERM", "KILL", "KILL"] and decoder._run.reaped
    finally:
        proc.code = -9
        _finish_fault_fake(decoder)


def test_claim_already_queued_does_not_retry_a_failed_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 30)
    queued, proceed = threading.Event(), threading.Event()
    proc = Child(unkillable=True)
    decoder = _aprs(proc)
    with _client(tmp_path, probe=_present, aprs_factory=lambda: decoder,
                 sweeper_factory=lambda: _StubSweeper([])) as client:
        decoder.start()
        arbiter = client.app.state.radio_arbiter
        reserve = arbiter.reserve

        @contextmanager
        def queued_reservation(*args):
            queued.set()
            assert proceed.wait(3)
            with reserve(*args):
                yield

        monkeypatch.setattr(arbiter, "reserve", queued_reservation)
        try:
            with ThreadPoolExecutor(max_workers=1) as claims:
                claim = claims.submit(client.get, "/api/spectrum")
                try:
                    assert queued.wait(2)
                    decoder.stop()
                    assert proc.sent == ["TERM", "KILL"]
                    assert decoder.release_failure() is not None
                finally:
                    proceed.set()
                assert claim.result(timeout=3).status_code == 503
            assert proc.sent == ["TERM", "KILL"]
            assert client.get("/api/hardware").json()["state"] == "busy"
        finally:
            proc.code = -9


@pytest.mark.parametrize("spawning", [False, True], ids=["live", "spawning"])
def test_shutdown_only_stops_a_live_or_opening_aprs_child_once(monkeypatch, spawning):
    spawn_entered, spawn_open, shutdown_waiting = (threading.Event() for _ in range(3))
    waits = []

    class Proc(Child):
        def wait(self, timeout=None):
            waits.append((tuple(self.sent), timeout))
            return super().wait(timeout)

    proc = Proc(["rtl_fm"], unkillable=True)
    decoder = _aprs(proc)

    def spawn(*_):
        spawn_entered.set()
        assert spawn_open.wait(5)
        return proc

    if spawning:
        decoder._spawn = spawn
    wait_physical = decoder._wait_physical

    def observe_wait(*args):
        shutdown_waiting.set()
        return wait_physical(*args)

    monkeypatch.setattr(decoder, "_wait_physical", observe_wait)
    try:
        with ThreadPoolExecutor(max_workers=2) as controls:
            start = controls.submit(decoder.start)
            try:
                if spawning:
                    assert spawn_entered.wait(3)
                else:
                    start.result(timeout=5)
                shutdown = controls.submit(decoder.shutdown)
                assert shutdown_waiting.wait(3)
            finally:
                spawn_open.set()

            if spawning:
                with pytest.raises(aprs.AprsUnavailable, match="receiver start was cancelled"):
                    start.result(timeout=8)
            with pytest.raises(aprs.AprsUnavailable, match="survived kill"):
                shutdown.result(timeout=8)

        run = decoder._run
        assert run.complete.wait(3)
        assert proc.sent == ["TERM", "KILL"]
        assert [sent for sent, _ in waits] == [("TERM",), ("TERM", "KILL")]
        assert sum(budget for _, budget in waits) <= receiver_lifecycle.PHYSICAL_WAIT_S == 7
        assert run.reap_failed and not run.reaped
    finally:
        proc.code = -9


@pytest.mark.parametrize("control", ["stop", "release", "retune", "shutdown"])
def test_control_during_periodic_retry_keeps_its_own_ticket(monkeypatch, control):
    # Advance only the lifecycle clock, leaving Event.wait and the child wait budgets real.
    now = [10.0]
    monkeypatch.setattr(receiver_lifecycle, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 30)
    timer_waiting, periodic_waiting, time_out, control_waiting = (
        threading.Event() for _ in range(4)
    )
    waits = []

    class Proc(Child):
        def wait(self, timeout=None):
            waits.append((len(self.sent), timeout))
            if len(self.sent) == 3:
                periodic_waiting.set()
                assert time_out.wait(5)
                raise subprocess.TimeoutExpired(self.args, timeout)
            return super().wait(timeout)

    proc = Proc(["rtl_fm"], unkillable=True)
    decoder = _aprs(proc)
    decoder.start()
    run = decoder._run
    wait_timer = run.retry_wake.wait

    def observe_timer(timeout):
        timer_waiting.set()
        return wait_timer(timeout)

    monkeypatch.setattr(run.retry_wake, "wait", observe_timer)
    decoder.stop()
    wait_physical = decoder._wait_physical

    def observe_control(run, intent, ticket):
        assert ticket is not None
        control_waiting.set()
        return wait_physical(run, intent, ticket)

    monkeypatch.setattr(decoder, "_wait_physical", observe_control)
    action = (lambda: decoder.retune(145_000_000)) if control == "retune" else (
        getattr(decoder, control))
    try:
        assert timer_waiting.wait(3)
        now[0] += 30
        run.retry_wake.set()
        assert periodic_waiting.wait(3)
        with ThreadPoolExecutor(max_workers=1) as controls:
            request = controls.submit(action)
            try:
                assert control_waiting.wait(3)
                assert not request.done()
                proc.unkillable = False
            finally:
                time_out.set()
            request.result(timeout=5)

        assert proc.sent == ["TERM", "KILL", "KILL", "KILL"]
        assert run.reaped and decoder.release_failure(pending=True) is None
        budget = decoder.STOP_WAIT_S if control == "shutdown" else (
            receiver_lifecycle.REAP_RETRY_WAIT_S)
        assert [phase for phase, _ in waits] == [1, 2, 3, 4]
        assert 0 < waits[-1][1] <= budget
    finally:
        time_out.set()
        proc.code = -9
        monkeypatch.setattr(decoder, "_wait_physical", wait_physical)
        _finish_fault_fake(decoder)


@pytest.mark.parametrize("owner", ["sweeper", "decoder"])
def test_queued_aprs_claim_does_not_retry_failed_legacy_stop(tmp_path, monkeypatch, owner):
    queued, proceed = threading.Event(), threading.Event()
    proc = Child(unkillable=True)
    receiver = (Sweeper(spawn=lambda *_: proc, radio_present=lambda: True)
                if owner == "sweeper" else _sensors(lambda *_: proc))
    launched = []

    def spawn_aprs(*_):
        launched.append(Child())
        return launched[-1]

    def aprs_factory():
        return aprs.AprsDecoder(spawn=spawn_aprs, radio_present=lambda: True, which=lambda t: t)

    with _client(tmp_path, probe=_present, aprs_factory=aprs_factory,
                 **{f"{owner}_factory": lambda: receiver}) as client:
        receiver.start()
        arbiter = client.app.state.radio_arbiter
        reserve = arbiter.reserve

        @contextmanager
        def queued_reservation(*args):
            queued.set()
            assert proceed.wait(5)
            with reserve(*args):
                yield

        monkeypatch.setattr(arbiter, "reserve", queued_reservation)
        try:
            with ThreadPoolExecutor(max_workers=1) as claims:
                claim = claims.submit(client.get, "/api/aprs")
                try:
                    assert queued.wait(3)
                    receiver.stop()
                    assert proc.sent == ["TERM", "KILL"]
                    reason = receiver.release_failure()
                    assert reason is not None
                finally:
                    proceed.set()
                response = claim.result(timeout=5)
            assert response.status_code == 503 and response.json()["reason"] == reason
            assert proc.sent == ["TERM", "KILL"]
            assert not launched
            assert client.get("/api/hardware").json()["state"] == "busy"
        finally:
            proc.code = -9


@pytest.mark.parametrize("broken", ["sweeper", "ppm"])
def test_shutdown_owner_probe_failure_does_not_hide_other_owners(broken, caplog):
    reports_at_close = []
    state = SimpleNamespace(**dict.fromkeys((*app.SDR_CONSUMERS, "lora", "flock", "rayhunter")))

    def consumer(name):
        def failure(*, pending=False):
            assert pending == (name != "ppm")
            if name == broken:
                raise OSError("ownership probe failed")
            return "still releasing the radio"

        return SimpleNamespace(stop=lambda: None, release_failure=failure)

    state.sweeper, state.decoder = consumer("sweeper"), consumer("decoder")
    state.ppm_calibration = consumer("ppm")
    state.radio_arbiter = radio_control.RadioArbiter()
    state.reception_sink = radio_control.ReceptionSink()
    state.progress = state.exam_store = SimpleNamespace(
        close=lambda: reports_at_close.append(tuple(caplog.messages)))
    errors = asyncio.run(app.shutdown_console_state(state))

    assert len(reports_at_close) == 2
    expected = {f"{name}: still releasing the radio" for name in ("sweeper", "decoder", "ppm")
                if name != broken}
    expected.add(f"{broken}: radio ownership could not be confirmed (ownership probe failed)")
    assert set(errors) == expected
    for lines in reports_at_close:
        assert all(f"console shutdown: {entry}" in lines for entry in expected)
