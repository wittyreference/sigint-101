# ABOUTME: Tests every decoder's child lifecycle: a dead tool reads stopped, backs off, is killed.
# ABOUTME: Fakes stand at the process boundary; one real pipeline proves the SIGTERM-trap kill path.

import io
import shlex
import signal
import subprocess
import threading

import pytest

from sigint101 import (
    acars,
    adsb,
    ais,
    aprs,
    apt,
    childproc,
    decode,
    dmr,
    flock,
    pocsag,
    rds,
    receiver_lifecycle,
    sstv,
)
from sigint101.childproc import Pipeline, command_line, spawn
from tests.child_fakes import USB_BUSY, Child, LateProc


class _Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


def _common():
    return {"radio_present": lambda: True, "which": lambda name: "/usr/bin/" + name}


# name -> (build(spawn, tmp_path), Unavailable, finite capture?)
DECODERS = {
    "decode": (lambda s, t: decode.Decoder(spawn=s, **_common()), decode.DecodeUnavailable, False),
    "aprs": (lambda s, t: aprs.AprsDecoder(spawn=s, **_common()), aprs.AprsUnavailable, False),
    "pocsag": (lambda s, t: pocsag.PocsagDecoder(spawn=s, **_common()),
               pocsag.PocsagUnavailable, False),
    "rds": (lambda s, t: rds.RDSDecoder(spawn=s, **_common()), rds.RDSUnavailable, False),
    "acars": (lambda s, t: acars.AcarsDecoder(spawn=s, **_common()),
              acars.AcarsUnavailable, False),
    "ais": (lambda s, t: ais.AisDecoder(spawn=s, **_common()), ais.AisUnavailable, False),
    "dmr": (lambda s, t: dmr.DmrDecoder(spawn=s, **_common()), dmr.DmrUnavailable, False),
    "adsb": (lambda s, t: adsb.AdsbDecoder(spawn=s, **_common()), adsb.AdsbUnavailable, False),
    "apt": (lambda s, t: apt.AptDecoder(spawn=s, output_dir=t / "apt", **_common()),
            apt.AptUnavailable, True),
    "sstv": (lambda s, t: sstv.SstvDecoder(spawn=s, output_dir=t / "sstv", data_dir=t,
                                           **_common()),
             sstv.SstvUnavailable, True),
    "flock": (lambda s, t: flock.FlockDetector(spawn=s, **_common()),
              flock.FlockUnavailable, False),
}


def _build(name, tmp_path, make_child):
    build, unavailable, _ = DECODERS[name]
    spawned = []

    def spawn_child(*_args):
        child = make_child()
        spawned.append(child)
        return child

    dec = build(spawn_child, tmp_path)
    clock = _Clock()
    dec.watch.clock = clock
    return dec, unavailable, spawned, clock


@pytest.fixture(autouse=True)
def _quick_stops(monkeypatch):
    # The real waits are seconds; the fakes answer at once or never, so a sliver is plenty.
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0.01)


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_a_child_that_dies_reads_stopped_and_is_not_respawned_every_poll(tmp_path, name):
    dec, unavailable, spawned, clock = _build(
        name, tmp_path, lambda: Child(["tool"], code=1, said=USB_BUSY))
    dec.start()
    dec.drain()
    assert dec.running is False

    # The next poll reports the death with the tool's own words, rather than respawning.
    with pytest.raises(unavailable) as first:
        dec.start()
    assert "exit status 1" in str(first.value)
    assert "usb_claim_interface error -6" in str(first.value)
    assert dec.watch.stopped is True
    assert dec.watch.retry_in() > 0

    # Polls inside the back-off keep saying so, and launch nothing.
    for _ in range(5):
        with pytest.raises(unavailable) as again:
            dec.start()
        assert "usb_claim_interface error -6" in str(again.value)
    assert len(spawned) == 1

    # Once the back-off runs out the console tries again — the dongle may have come free.
    clock.now += dec.watch.retry_in()
    dec.start()
    assert len(spawned) == 2
    dec.stop()


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_a_signal_death_is_reported_by_name(tmp_path, name):
    dec, unavailable, _, _ = _build(
        name, tmp_path, lambda: Child(["tool"], code=-signal.SIGKILL))
    dec.start()
    dec.drain()
    with pytest.raises(unavailable, match="killed by SIGKILL"):
        dec.start()


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_stop_escalates_to_kill_for_a_child_that_ignores_sigterm(tmp_path, name):
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(["tool"], stubborn=True))
    dec.start()
    dec.stop()
    assert spawned[0].sent == ["TERM", "KILL"]
    assert spawned[0].poll() == -signal.SIGKILL
    assert dec.running is False
    assert dec.error is None


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_stop_says_so_when_a_child_survives_kill(tmp_path, name):
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(["tool"], unkillable=True))
    dec.start()
    dec.stop()
    assert spawned[0].sent == ["TERM", "KILL"]
    assert "dongle" in dec.error or "interface" in dec.error


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_a_deliberate_stop_is_not_reported_as_a_death(tmp_path, name):
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(["tool"]))
    dec.start()
    dec.stop()
    dec.drain()
    assert dec.watch.stopped is False
    dec.start()                     # no back-off: the console stopped it, nothing died
    assert len(spawned) == 2
    dec.stop()


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_live_command_is_read_off_the_running_child(tmp_path, name):
    argv = ["rtl_433", "-f", "433920000", "-F", "json"]
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(argv))
    assert dec.live_command() is None           # nothing runs yet
    dec.start()
    assert dec.live_command() == "rtl_433 -f 433920000 -F json"
    dec.stop()
    assert dec.live_command() is None           # nothing runs any more


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_live_command_is_none_once_the_child_has_died(tmp_path, name):
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(["rtl_433"], code=1))
    dec.start()
    assert dec.live_command() is None


def test_a_streaming_pipeline_with_a_dead_first_stage_is_not_running(tmp_path):
    # rtl_fm died; multimon-ng has not noticed yet. Nothing is being decoded, so not running.
    def make():
        return Pipeline(Child(["rtl_fm"], code=1, said=USB_BUSY), Child(["multimon-ng"]))
    dec, unavailable, _, _ = _build("aprs", tmp_path, make)
    dec.start()
    assert dec.running is False
    with pytest.raises(unavailable, match="rtl_fm stopped"):
        dec.start()


def test_an_sstv_capture_that_finishes_is_not_a_death(tmp_path):
    # The capture is finite by design: sox trims, sstv decodes and exits 0, rtl_fm gets SIGPIPE.
    def make():
        return Pipeline(Child(["rtl_fm"], code=-signal.SIGPIPE), Child(["sox"], code=0),
                        Child(["sstv"], code=0), finite=True)
    dec, _, spawned, _ = _build("sstv", tmp_path, make)
    dec.start()
    dec.drain()
    dec.start()                     # the next capture begins at once
    assert len(spawned) == 2
    assert dec.watch.stopped is False


def test_an_apt_pass_that_times_out_cleanly_is_not_a_death(tmp_path):
    dec, _, spawned, _ = _build("apt", tmp_path, lambda: Child(["satdump"], code=0))
    dec.start()
    dec.start()
    assert len(spawned) == 2
    assert dec.watch.stopped is False


# --- C52: a retune starts a new run; the death of the run it cancelled is not that run's answer --
# WHY: on the reference device (R23) rtl_fm was killed during an APRS decode and the frequency was
# changed at once. The first poll at the new frequency answered with the old run's death, and only
# the next poll showed the new run. The retune cancels the run that died, so its death, its error
# and its back-off end with it; the next start launches at the new frequency.

RETUNABLE = {"acars": 131_725_000, "aprs": 144_800_000, "dmr": 451_000_000,
             "pocsag": 929_662_500, "rds": 94_700_000}


def _build_tuned(name, tmp_path):
    """A decoder that records the frequency of each spawn; its tools run until a test kills them."""
    build, unavailable, _ = DECODERS[name]
    spawned, tuned = [], []

    def spawn_child(freq_hz, _device):
        spawned.append(Child(["rtl_fm", "-f", str(freq_hz)]))
        tuned.append(freq_hz)
        return spawned[-1]

    dec = build(spawn_child, tmp_path)
    dec.watch.clock = _Clock()      # the back-off never runs out by itself
    return dec, unavailable, spawned, tuned


@pytest.mark.parametrize("name", sorted(RETUNABLE))
def test_a_retune_after_a_death_starts_the_new_run_not_the_old_death(tmp_path, name):
    dec, _, spawned, tuned = _build_tuned(name, tmp_path)
    home = dec.freq_hz
    dec.start()
    spawned[0].kill()                           # the tool is killed under the decode
    dec.drain()                                 # and its death is recorded before the retune
    assert dec.watch.stopped is True
    dec.retune(RETUNABLE[name])
    assert dec.error is None
    assert dec.watch.stopped is False and dec.watch.retry_in() is None
    dec.start()                                 # the first poll at the new frequency
    assert tuned == [home, RETUNABLE[name]]
    assert dec.running
    dec.stop()


@pytest.mark.parametrize("name", sorted(RETUNABLE))
def test_a_late_diagnostic_of_the_old_death_does_not_return_after_the_retune(tmp_path, name):
    # The exit diagnostic runs on its own thread and can land after the retune; it must not put
    # the old death back on record.
    dec, _, spawned, tuned = _build_tuned(name, tmp_path)
    enrich = dec.watch.enrich
    found, go_on, enriched = threading.Event(), threading.Event(), threading.Event()

    def held_enrich(proc, reason):
        found.set()
        assert go_on.wait(5)
        enrich(proc, reason)
        enriched.set()

    dec.watch.enrich = held_enrich
    dec.start()
    spawned[0].kill()
    assert found.wait(5)                        # the death is recorded; its diagnostic waits
    dec.retune(RETUNABLE[name])
    go_on.set()
    assert enriched.wait(5)
    assert dec.error is None and dec.watch.reason is None
    dec.start()
    assert tuned[-1] == RETUNABLE[name] and len(spawned) == 2
    dec.stop()


@pytest.mark.parametrize("name", sorted(RETUNABLE))
def test_a_retune_to_the_same_frequency_still_reports_the_death(tmp_path, name):
    # Tuning to where the decoder already is changes nothing, so the death is still told once.
    dec, unavailable, spawned, _ = _build_tuned(name, tmp_path)
    dec.start()
    spawned[0].kill()
    dec.drain()
    dec.retune(dec.freq_hz)
    with pytest.raises(unavailable, match="killed by SIGKILL"):
        dec.start()
    assert len(spawned) == 1


@pytest.mark.parametrize("name", sorted(RETUNABLE))
def test_a_tool_that_survived_kill_still_refuses_after_a_retune(tmp_path, name, monkeypatch):
    # The death is the old run's, but a stage that would not die may still hold the dongle: that
    # cleanup failure is the radio's state now, and the retune must not clear it.
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.05)
    build, unavailable, _ = DECODERS[name]
    stages = []

    def spawn_pipeline(*_args):
        stages.append((Child(["rtl_fm"]), Child(["multimon-ng"], unkillable=True)))
        return Pipeline(*stages[-1])

    dec = build(spawn_pipeline, tmp_path)
    dec.watch.clock = _Clock()
    dec.EOF_WAIT_S = 0.01
    dec.start()
    run = dec._run
    stages[0][0].kill()                         # rtl_fm is killed; multimon-ng cannot be
    assert run.physical.wait(5)
    assert "survived kill" in run.failure
    dec.retune(RETUNABLE[name])
    with pytest.raises(unavailable, match="survived kill; the dongle may still be held"):
        dec.start()
    assert len(stages) == 1
    stages[0][1].unkillable = False             # let the supervisor's retry reap it
    assert run.complete.wait(5)


def test_rtl_433_that_survived_kill_still_refuses_after_a_retune(tmp_path):
    dec, unavailable, spawned, _ = _build("decode", tmp_path,
                                          lambda: Child(["rtl_433"], unkillable=True))
    dec.start()
    dec.retune(315_000_000)                     # the stop inside the retune cannot kill it
    with pytest.raises(unavailable, match="survived kill; the dongle may still be held"):
        dec.start()
    assert len(spawned) == 1


# --- C52 round 2: the back-off across a retune (ruling (a)) and rtl_433's decoder -------------
# A retune lifts the wait of a single death, so one kill and a retune start at once. From the
# second quick death in a row the back-off has grown (childproc's FIRST_RETRY_S doubles), and
# that wait survives the retune, with a reason that names the frequency the next try is at.

LATE_TUNED = {**RETUNABLE, "decode": 315_000_000}
OTHER_HZ = {"acars": 131_550_000, "aprs": 145_010_000, "dmr": 452_000_000,
            "pocsag": 152_840_000, "rds": 101_100_000, "decode": 868_000_000}


@pytest.fixture
def late_tuned(tmp_path):
    """Decoders whose tools hold their pipe open until killed, as a real one does."""
    procs = []

    def make(name):
        build, unavailable, _ = DECODERS[name]
        tuned = []
        holder = []

        def spawn_late(*_args):
            tuned.append(holder[0].freq_hz)
            procs.append(LateProc(["rtl_fm", "-f", str(tuned[-1])]))
            return procs[-1]

        dec = build(spawn_late, tmp_path)
        holder.append(dec)
        clock = _Clock()
        dec.watch.clock = clock
        return dec, unavailable, procs, tuned, clock

    yield make
    for proc in procs:
        proc.close_pipe()


def _kill(dec, proc):
    """The tool is killed: it exits and its pipe closes, and the decoder reads its end."""
    proc.die(-signal.SIGKILL)
    proc.close_pipe()
    dec.drain()


def test_rtl_433_retuned_after_a_death_starts_at_once_at_the_new_frequency(late_tuned):
    dec, _, procs, tuned, _ = late_tuned("decode")
    home = dec.freq_hz
    dec.start()
    _kill(dec, procs[0])
    assert dec.watch.stopped is True
    dec.retune(LATE_TUNED["decode"])
    assert dec.error is None and dec.watch.retry_in() is None
    dec.start()                                 # no wait: the first poll there spawns
    assert tuned == [home, LATE_TUNED["decode"]]
    assert dec.running
    dec.stop()


def test_rtl_433_retuned_to_the_same_frequency_still_reports_the_death(late_tuned):
    dec, unavailable, procs, _, _ = late_tuned("decode")
    dec.start()
    _kill(dec, procs[0])
    dec.retune(dec.freq_hz)
    with pytest.raises(unavailable, match="killed by SIGKILL"):
        dec.start()
    assert len(procs) == 1


@pytest.mark.parametrize("name", sorted(LATE_TUNED))
def test_quick_deaths_across_retunes_still_wait_and_say_where_the_next_try_is(late_tuned, name):
    dec, unavailable, procs, tuned, clock = late_tuned(name)
    dec.start()
    _kill(dec, procs[0])                        # the first quick death: 2 s
    dec.retune(LATE_TUNED[name])
    dec.start()                                 # a single death's wait is lifted
    assert len(procs) == 2
    _kill(dec, procs[1])                        # the second in a row: the wait has grown to 4 s
    dec.retune(OTHER_HZ[name])
    assert dec.watch.retry_in() == 2 * childproc.FIRST_RETRY_S
    where = f"{OTHER_HZ[name] / 1e6:.4f} MHz"
    with pytest.raises(unavailable) as held:
        dec.start()
    assert str(held.value) == f"{dec.watch.tool} keeps dying; it tries again at {where}"
    assert "SIGKILL" not in str(held.value)     # the old death is not told as this frequency's
    assert len(procs) == 2
    clock.now += dec.watch.retry_in()
    dec.start()                                 # once the wait runs out, it tries there
    assert tuned[-1] == OTHER_HZ[name] and len(procs) == 3
    dec.stop()


def _unseen_by_the_supervisor(child, run):
    """The supervisor's polls find the child alive until the run is cancelled; others see it."""
    real = child.poll

    def poll():
        if threading.current_thread().name.endswith("-supervisor") and not run.cancel.is_set():
            return None
        return real()

    child.poll = poll


@pytest.mark.parametrize("name", sorted(RETUNABLE))
def test_quick_deaths_the_supervisor_has_not_seen_yet_still_count_at_a_retune(tmp_path, name):
    # Codex D-091 F6: the child has exited, but its supervisor has not observed it when the
    # retune fences the run. The exit is visible, so the retune counts it before the fence.
    dec, unavailable, spawned, tuned = _build_tuned(name, tmp_path)
    clock = dec.watch.clock
    dec.start()
    _unseen_by_the_supervisor(spawned[0], dec._run)
    spawned[0].kill()
    dec.retune(RETUNABLE[name])
    assert dec.watch.retry_in() is None         # one quick death: its wait is lifted
    dec.start()
    assert len(spawned) == 2
    _unseen_by_the_supervisor(spawned[1], dec._run)
    spawned[1].kill()
    dec.retune(OTHER_HZ[name])                  # the second in a row: its grown wait stands
    assert dec.watch.retry_in() == 2 * childproc.FIRST_RETRY_S
    with pytest.raises(unavailable, match="keeps dying"):
        dec.start()
    assert len(spawned) == 2
    clock.now += dec.watch.retry_in()
    dec.start()
    _unseen_by_the_supervisor(spawned[2], dec._run)
    spawned[2].kill()
    dec.retune(RETUNABLE[name])                 # the third: the wait keeps growing
    assert dec.watch.retry_in() == 4 * childproc.FIRST_RETRY_S
    with pytest.raises(unavailable, match="keeps dying"):
        dec.start()
    assert tuned == [tuned[0], RETUNABLE[name], OTHER_HZ[name]]


@pytest.mark.parametrize("name", sorted(RETUNABLE))
def test_a_death_the_supervisor_recorded_is_not_counted_again_by_the_retune(tmp_path, name):
    # The other order: the supervisor has recorded the death and is held before it fences the
    # run when the retune looks for a visible exit. One death, one count: its wait is lifted.
    dec, _, spawned, tuned = _build_tuned(name, tmp_path)
    needs_diagnostics = dec._needs_diagnostics
    recorded, go_on = threading.Event(), threading.Event()

    def held(run):
        recorded.set()
        assert go_on.wait(5)
        return needs_diagnostics(run)

    dec._needs_diagnostics = held
    dec.start()
    run = dec._run
    spawned[0].kill()
    assert recorded.wait(5)                     # recorded under _life; the run is not fenced
    retune = threading.Thread(target=dec.retune, args=(RETUNABLE[name],))
    retune.start()
    assert run.cancel.wait(5)                   # the retune has looked, cancelled and fenced
    go_on.set()
    retune.join(5)
    assert not retune.is_alive()
    assert dec.watch.retry_in() is None and dec.watch.reason is None
    dec.start()
    assert tuned[-1] == RETUNABLE[name] and len(spawned) == 2
    dec.stop()


def test_rtl_433_deaths_its_reader_has_not_recorded_yet_still_count_at_a_retune(late_tuned):
    # Codex D-091 F6, rtl_433's path: the child has exited but its pipe is still open, so the
    # reader has recorded nothing when the retune marks the watch stopping.
    dec, unavailable, procs, tuned, clock = late_tuned("decode")
    dec.STOP_WAIT_S = 0.01
    dec.start()
    procs[0].die(-signal.SIGKILL)
    dec.retune(LATE_TUNED["decode"])
    assert dec.watch.retry_in() is None
    dec.start()
    assert len(procs) == 2
    procs[1].die(-signal.SIGKILL)
    dec.retune(OTHER_HZ["decode"])
    assert dec.watch.retry_in() == 2 * childproc.FIRST_RETRY_S
    with pytest.raises(unavailable, match="keeps dying"):
        dec.start()
    assert len(procs) == 2
    clock.now += dec.watch.retry_in()
    dec.start()
    procs[2].die(-signal.SIGKILL)
    dec.retune(LATE_TUNED["decode"])
    assert dec.watch.retry_in() == 4 * childproc.FIRST_RETRY_S
    with pytest.raises(unavailable, match="keeps dying"):
        dec.start()
    assert len(procs) == 3


def test_rtl_433_that_survived_kill_refuses_every_start_until_it_is_reaped(tmp_path):
    # Codex D-091 F7: the first refusal used to consume the only marker, and the second start
    # spawned beside the live rtl_433.
    dec, unavailable, spawned, _ = _build("decode", tmp_path,
                                          lambda: Child(["rtl_433"], unkillable=True))
    dec.start()
    dec.retune(315_000_000)                     # the stop inside the retune cannot kill it
    for _ in range(3):
        with pytest.raises(unavailable, match="survived kill; the dongle may still be held"):
            dec.start()
    assert len(spawned) == 1                    # nothing spawned beside the live child
    assert dec.release_failure() == decode.SURVIVED_KILL     # and the handoff check says so
    assert dec.release_failure(pending=True) == decode.SURVIVED_KILL
    spawned[0].code = -signal.SIGKILL           # the kill lands once it leaves the kernel
    assert dec.release_failure() is None
    dec.start()                                 # reaped: the radio is free again
    assert len(spawned) == 2 and dec.error is None
    spawned[1].unkillable = False
    dec.stop()


# --- C52 round 4 (Codex D-093): the legacy decoder's retune and stop at the radio boundary -----


def test_rtl_433_retune_counts_a_status_exit_without_waiting_on_its_diagnostic(
        late_tuned, monkeypatch):
    # F1: a tool that exits on its own (status 1, not a signal) has its stderr quoted. The retune
    # counts the exit from its status alone and leaves the words to the diagnostic path.
    entered, resume = threading.Event(), threading.Event()

    def stalled_tail(_stage):
        entered.set()
        resume.wait(10)
        return []

    monkeypatch.setattr(childproc, "stderr_tail", stalled_tail)
    dec, unavailable, procs, tuned, _ = late_tuned("decode")
    dec.STOP_WAIT_S = 0.01

    def retune_unheld(hz):
        retune = threading.Thread(target=dec.retune, args=(hz,), daemon=True)
        retune.start()
        retune.join(3)
        assert not retune.is_alive(), "the retune waited on the exit diagnostic"

    try:
        dec.start()
        procs[0].die(1)                         # pipe still open: the reader has seen nothing
        retune_unheld(LATE_TUNED["decode"])
        assert dec.watch.retry_in() is None     # one death, counted, its wait lifted
        dec.start()
        procs[1].die(1)
        retune_unheld(OTHER_HZ["decode"])
        assert dec.watch.retry_in() == 2 * childproc.FIRST_RETRY_S    # the count advanced
        with pytest.raises(unavailable, match="keeps dying"):
            dec.start()
        assert not entered.is_set()             # no stderr was read on the way
    finally:
        resume.set()


class _WaitFails(Child):
    """A tool that ignores SIGTERM and whose stop's reap wait fails with an OS error."""

    def __init__(self):
        super().__init__(["rtl_433"], stubborn=True)

    def wait(self, timeout=None):
        if self.code is None and "TERM" in self.sent:
            raise OSError("wait failed")
        return super().wait(timeout)


def test_rtl_433_whose_stop_wait_fails_is_kept_until_its_exit_is_confirmed(tmp_path):
    # F2: the wait error skipped keeping the child, so a live rtl_433 was dropped and the radio
    # handed on.
    dec, unavailable, spawned, _ = _build("decode", tmp_path, _WaitFails)
    dec.start()
    dec.retune(315_000_000)                     # the error is the stop's, not the caller's
    for _ in range(3):
        with pytest.raises(unavailable, match="could not be confirmed stopped"):
            dec.start()
    assert len(spawned) == 1
    reason = dec.release_failure()
    assert reason == dec.release_failure(pending=True)
    assert "wait failed" in reason and "the dongle may still be held" in reason
    spawned[0].code = -signal.SIGKILL           # its exit shows at last
    assert dec.release_failure(pending=True) is None
    dec.start()
    assert len(spawned) == 2 and dec.error is None
    spawned[1].code = 0
    dec.stop()


class _SlowToStop(Child):
    """A tool whose stop is in flight: SIGTERM lands, and the reap waits for a gate."""

    def __init__(self):
        super().__init__(["rtl_433"])
        self.waiting, self.gate = threading.Event(), threading.Event()

    def terminate(self):
        self.sent.append("TERM")

    def wait(self, timeout=None):
        if self.code is None and "TERM" in self.sent:
            self.waiting.set()
            if not self.gate.wait(5):
                raise subprocess.TimeoutExpired("rtl_433", timeout)
            self.code = -signal.SIGTERM
        return super().wait(timeout)


def test_rtl_433_reports_its_stop_pending_until_the_reap_is_confirmed(tmp_path):
    # F3: while a stop is waiting on the reap, the child is in neither ownership field.
    dec, _, spawned, _ = _build("decode", tmp_path, _SlowToStop)
    dec.start()
    retune = threading.Thread(target=dec.retune, args=(315_000_000,), daemon=True)
    retune.start()
    try:
        assert spawned[0].waiting.wait(5)       # the stop is waiting on the reap
        assert dec.release_failure(pending=True) == "rtl_433 is still releasing the radio"
        assert dec.release_failure() is None    # as the supervised receivers: only pending says
    finally:
        spawned[0].gate.set()
        retune.join(5)
    assert not retune.is_alive()
    assert dec.release_failure(pending=True) is None
    dec.start()
    assert len(spawned) == 2


@pytest.mark.parametrize("name", sorted(LATE_TUNED))
def test_a_retune_after_the_grown_wait_ran_out_starts_at_once(late_tuned, name):
    # The wait only survives while it runs: a retune after it has run out is never held.
    dec, _, procs, tuned, clock = late_tuned(name)
    dec.start()
    _kill(dec, procs[0])
    dec.retune(LATE_TUNED[name])
    dec.start()
    _kill(dec, procs[1])
    clock.now += 2 * childproc.FIRST_RETRY_S
    dec.retune(OTHER_HZ[name])
    assert dec.watch.retry_in() is None and dec.watch.reason is None
    dec.start()
    assert tuned[-1] == OTHER_HZ[name] and len(procs) == 3
    dec.stop()


def test_a_real_pipeline_that_traps_sigterm_is_killed_on_stop(tmp_path, monkeypatch):
    # The audit's probe (T-06): both halves trap SIGTERM, and stop() used to return after 9 s
    # with both alive. Now it escalates, and neither survives.
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.3)
    procs = []

    def real_pipeline(freq, device):
        first = spawn(["bash", "-c", "trap '' TERM; while :; do sleep 0.1; done"])
        second = spawn(["bash", "-c", "trap '' TERM; cat >/dev/null"], stdin=first.stdout,
                       text=True)
        first.stdout.close()
        procs.extend([first, second])
        return Pipeline(first, second)

    dec = aprs.AprsDecoder(spawn=real_pipeline, **_common())
    dec.start()
    try:
        dec.stop()
        assert all(p.poll() is not None for p in procs)
        assert dec.error is None
    finally:
        for p in procs:
            if p.poll() is None:  # pragma: no cover - only when the assertion above failed
                p.kill()
                p.wait(timeout=3)
        # Physical stop returns before the supervisor and reader finish closing streams.
        ticket = dec._run
        assert ticket.complete.wait(5)
        ticket.reader.join(5)
        assert not ticket.reader.is_alive()
        for p in procs:
            for stream in (p.stdout, p.stderr):
                if stream is not None:
                    stream.close()


# --- every real launcher goes through childproc.spawn and carries its real argv ---------------


class _Launched:
    """What childproc.spawn hands back, recorded at the boundary instead of exec'd."""

    def __init__(self, argv, kw, code):
        self.args = list(argv)
        self.kw = kw
        self.code = code
        self.stdout = io.BytesIO()
        self.stderr = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -signal.SIGTERM

    def kill(self):
        self.code = -signal.SIGKILL

    def wait(self, timeout=None):
        return self.code


def _record_spawns(monkeypatch, module, code=None):
    seen = []

    def fake_spawn(argv, **kw):
        proc = _Launched(argv, kw, code)
        seen.append(proc)
        return proc

    monkeypatch.setattr(module, "spawn", fake_spawn)
    return seen


def _joined(seen):
    return " | ".join(shlex.join(p.args) for p in seen)


def test_decode_launches_rtl_433_through_spawn(monkeypatch):
    seen = _record_spawns(monkeypatch, decode)
    cmd = decode.Decoder(freq_hz=433_920_000, device=0).command()
    proc = decode._popen(cmd)
    assert seen[0].args == cmd
    assert command_line(proc) == shlex.join(cmd)


@pytest.mark.parametrize("module,tools", [
    (aprs, ("rtl_fm", "multimon-ng")),
    (pocsag, ("rtl_fm", "multimon-ng")),
    (rds, ("rtl_fm", "redsea")),
])
def test_rtl_fm_pipelines_launch_both_stages_through_spawn(monkeypatch, module, tools):
    seen = _record_spawns(monkeypatch, module)
    pipe = module._popen(144_390_000, 0)
    assert [p.args[0] for p in seen] == list(tools)
    assert "144390000" in seen[0].args
    assert seen[1].kw["stdin"] is seen[0].stdout       # rtl_fm feeds the decoder
    assert command_line(pipe) == _joined(seen)
    assert " | " in command_line(pipe)


def test_sstv_launches_its_three_stage_capture_through_spawn(monkeypatch, tmp_path):
    seen = _record_spawns(monkeypatch, sstv)
    pipe = sstv._popen(145_800_000, 0, tmp_path / "x.png")
    assert [p.args for p in seen] == sstv.pipeline_commands(145_800_000, 0, tmp_path / "x.png")
    assert command_line(pipe) == _joined(seen)
    assert pipe.finite is True


@pytest.mark.parametrize("launch,tool", [
    (lambda tmp: acars._popen(131_550_000, 0), "acarsdec"),
    (lambda tmp: dmr._popen(450_000_000, 0), "dsd-fme"),
    (lambda tmp: apt._popen(137_100_000, 0, tmp), "satdump"),
    (lambda tmp: flock._popen("wlan1"), "tcpdump"),
])
def test_single_tools_launch_through_spawn(monkeypatch, tmp_path, launch, tool):
    module = {"acarsdec": acars, "dsd-fme": dmr, "satdump": apt, "tcpdump": flock}[tool]
    seen = _record_spawns(monkeypatch, module)
    proc = launch(tmp_path)
    assert seen[0].args[0] == tool
    assert command_line(proc) == shlex.join(seen[0].args)


def test_ais_launches_rtl_ais_through_spawn(monkeypatch):
    seen = _record_spawns(monkeypatch, ais)
    feed = ais._popen(0, "127.0.0.1", 0)
    try:
        assert seen[0].args[0] == "rtl_ais"
        assert command_line(feed) == shlex.join(seen[0].args)
    finally:
        feed.terminate()


def test_the_sweep_launches_rtl_power_through_spawn(monkeypatch):
    # rtl_power holds the dongle like any decoder: own session, and gone if the console is.
    from sigint101 import spectrum
    seen = _record_spawns(monkeypatch, spectrum)
    cmd = ["rtl_power", "-f", "88000000:108000000:125000", "-i", "1", "-d", "0"]
    proc = spectrum._popen(cmd)
    try:
        assert seen[0].args == cmd
        assert proc.stderr is not None      # its stderr still lands where the sweeper reads it
    finally:
        if proc.stderr is not None:
            proc.stderr.close()


def test_adsb_launch_failure_stops_dump1090_and_says_so(monkeypatch, adsb_ports):
    # The launch is refused, then the supervisor owns cleanup of the child and its diagnostics.
    from contextlib import nullcontext

    seen = _record_spawns(monkeypatch, adsb, code=1)
    monkeypatch.setattr(adsb, "_sbs_peer", lambda _: nullcontext(lambda _: True), raising=False)
    receiver = adsb.AdsbDecoder(**_common())
    with pytest.raises(adsb.AdsbUnavailable):
        receiver.start()
    assert seen[0].args[0] == "dump1090-fa"
    assert seen[0].args[seen[0].args.index("--net-sbs-port") + 1] == str(adsb_ports)
    assert receiver._run.proc.procs == tuple(seen)
    assert receiver._run.complete.wait(3) and receiver._run.reaped
