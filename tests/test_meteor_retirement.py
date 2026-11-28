# ABOUTME: Proves Meteor release, watchdog, EOF, and teardown ordering using gated child fakes.
# ABOUTME: The radio proof and the final result advance independently for the exact same run.

import shlex
import signal
import subprocess
import threading

import pytest

from sigint101 import receiver_lifecycle
from tests.test_meteor import MARKER, eventually, progress_line
from tests.test_meteor import meteor_rig as meteor_rig


def _start(rig):
    decoder, made, _ = rig
    decoder.start()
    return decoder, made[-1][0], decoder._run


def _release(decoder):
    thread = threading.Thread(target=decoder.release, daemon=True)
    thread.start()
    return thread


def test_meteor_release_before_proof_waits_outside_locks(rig):
    decoder, proc, run = _start(rig)
    thread = _release(decoder)
    assert proc.term.wait(2)
    assert decoder.release_failure(pending=True)
    assert not run.cancel.is_set()
    assert decoder._life.acquire(timeout=1)
    try:
        with decoder._runs:
            proc.feed(MARKER)
            assert run.physical.wait(1), "marker needed the lifecycle or reader guard"
            thread.join(1)
    finally:
        decoder._life.release()
    assert not thread.is_alive()
    assert not run.cancel.is_set() and not run.reaped
    assert proc.sent.count("TERM") == 1 and "KILL" not in proc.sent
    assert decoder.release_failure(pending=True) is None


def test_meteor_release_kills_only_when_proof_expires(rig):
    decoder, proc, run = _start(rig)
    _, _, settings = rig
    settings["now"] = 100
    first = _release(decoder)
    assert proc.term.wait(2)
    second = _release(decoder)
    assert not proc.killed.is_set()
    settings["now"] += 3.1
    run.wake.set()
    assert proc.killed.wait(2)
    assert run.complete.wait(2)
    first.join(2)
    second.join(2)
    assert proc.sent == ["TERM", "KILL"]
    assert run.physical.is_set() and run.cancel.is_set()
    assert decoder.snapshot()["state"] == "interrupted"


def test_meteor_failed_reap_refuses_until_retry(rig, monkeypatch):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.02)
    rig[2]["resists_kill"] = True
    decoder, proc, run = _start(rig)
    thread = _release(decoder)
    assert proc.killed.wait(2)
    proc.picture()
    proc.feed(MARKER)
    proc.close_pipe()
    thread.join(2)
    assert not thread.is_alive()
    eventually(lambda: run.reap_failed)
    assert not run.physical.is_set() and not run.reaped
    assert "dongle may still be held" in decoder.release_failure()
    proc.resists_kill = False
    assert run.complete.wait(2)
    assert run.reaped and run.physical.is_set()
    assert proc.sent[0] == "TERM" and set(proc.sent[1:]) == {"KILL"}
    assert decoder.release_failure(pending=True) is None
    assert decoder.snapshot()["state"] == "interrupted"


@pytest.mark.parametrize("kind", ["spawn-pending", "partial-launch"])
def test_meteor_release_during_spawn_stays_busy_until_reap(rig, monkeypatch, kind):
    from sigint101 import meteor

    decoder, made, settings = rig
    settings["resists_kill"] = True
    monkeypatch.setattr(receiver_lifecycle, "PHYSICAL_WAIT_S", 0.5)
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.02)
    entered, proceed = threading.Event(), threading.Event()
    spawn = decoder._spawn

    def gated_spawn(*args, **kwargs):
        proc = spawn(*args, **kwargs)
        if kind == "partial-launch":
            proc.stderr = None
        entered.set()
        proceed.wait(3)
        return proc

    monkeypatch.setattr(decoder, "_spawn", gated_spawn)
    errors = []

    def start():
        try:
            decoder.start()
        except meteor.MeteorUnavailable as exc:
            errors.append(str(exc))

    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    assert entered.wait(2)
    release = _release(decoder)
    eventually(lambda: decoder.release_failure(pending=True))
    proc = made[0][0]
    assert proc.sent == []
    proceed.set()
    assert proc.killed.wait(2)
    eventually(lambda: decoder._run.reap_failed)
    assert not decoder._run.physical.is_set()
    assert decoder.release_failure(pending=True)
    release.join(2)
    starter.join(2)
    assert errors and not starter.is_alive() and not release.is_alive()
    proc.resists_kill = False
    assert decoder._run.complete.wait(3)
    assert decoder._run.reaped and decoder.release_failure(pending=True) is None
    assert proc.sent and set(proc.sent) == {"KILL"}


@pytest.mark.parametrize("marker", ["early-marker", "no-marker"])
def test_meteor_finishing_watchdog_kills_exact_group(rig, marker):
    from sigint101 import meteor

    settings = rig[2]
    settings.update(now=100.0, resists_kill=marker == "no-marker")
    decoder, proc, run = _start(rig)
    if marker == "early-marker":
        settings["now"] += 10
        proc.feed(MARKER)
        assert run.physical.wait(2)
        deadline = 110 + meteor.FINISH_CAP_S
    else:
        deadline = 100 + 900 + meteor.FINISH_CAP_S
    settings["now"] = deadline - 0.01
    run.wake.set()
    assert not proc.killed.wait(0.03)
    settings["now"] = deadline
    run.wake.set()
    assert proc.killed.wait(2)
    assert "TERM" not in proc.sent
    if marker == "no-marker":
        assert not run.physical.is_set() and decoder.release_failure(pending=True)
        proc.feed(MARKER)
        proc.die(-signal.SIGKILL)
        proc.close_pipe()
    assert run.complete.wait(4)
    assert decoder.snapshot()["ended_by"] == "watchdog"
    assert decoder.snapshot()["state"] == "interrupted"
    assert "retry_in" not in decoder.snapshot()
    decoder.start()
    assert rig[1][-1][0].sent == []


@pytest.mark.parametrize("phase", ["starting", "receiving", "decoding"])
def test_meteor_shutdown_kills_directly_without_publishing(rig, phase):
    rig[2]["now"] = 100
    decoder, proc, _ = _start(rig)
    proc.picture()
    if phase == "receiving":
        rig[2]["now"] += 2
        proc.feed(progress_line())
    elif phase == "decoding":
        proc.feed(MARKER)
    eventually(lambda: decoder.snapshot()["state"] == phase)
    decoder.shutdown()
    assert "TERM" not in proc.sent and "KILL" in proc.sent
    body = decoder.snapshot()
    assert body["state"] == "interrupted" and body["ended_by"] == "shutdown"
    assert body["passes"] == [] and "retry_in" not in body


@pytest.mark.parametrize("kind", ["natural-exit", "release-quick-exit"])
def test_meteor_exit_drains_a_queued_marker_before_classification(rig, kind):
    decoder, proc, run = _start(rig)
    thread = None
    if kind == "release-quick-exit":
        thread = _release(decoder)
        assert proc.term.wait(2)
    proc.picture()
    proc.die(0)
    run.wake.set()
    assert run.physical.wait(2)
    if thread:
        thread.join(1)
        assert not thread.is_alive()
    assert not run.ready.is_set()
    proc.feed(MARKER)
    proc.close_pipe()
    assert run.complete.wait(2)
    assert decoder.snapshot()["state"] == "done"
    assert decoder.snapshot()["ended_by"] == ("handoff" if thread else "self_exit")


def test_meteor_eof_deadline_fences_late_reader(rig, monkeypatch):
    from sigint101 import meteor

    monkeypatch.setattr(receiver_lifecycle, "STREAM_EOF_S", 0.05)
    decoder, proc, run = _start(rig)
    proc.picture()
    proc.die(0)
    assert run.complete.wait(2)
    body = decoder.snapshot()
    assert body["state"] == "interrupted"
    assert "stderr" in body["reason"]
    assert not proc.closed.is_set(), "the supervisor closed the reader-owned pipe"
    decoder.start()
    proc.feed(MARKER, progress_line())
    proc.close_pipe()
    assert proc.closed.wait(2)
    assert not decoder._run.physical.is_set()
    assert decoder.snapshot()["state"] in ("starting", "searching")
    assert decoder.live_command() == shlex.join(rig[1][-1][0].args)
    assert isinstance(decoder, meteor.MeteorDecoder)


@pytest.mark.parametrize("kind", ["marker-picture", "marker-empty",
                                  "markerless-picture", "markerless-empty"])
def test_meteor_clean_exit_classifies_the_gallery(rig, kind):
    decoder, proc, run = _start(rig)
    if "marker-" in kind:
        proc.feed(MARKER)
    if kind.endswith("picture"):
        proc.picture()
    proc.picture("intermediate.png")
    outside = run.args[0].output.parent / "unrelated.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"outside")
    (run.args[0].output / "MSU-MR" / "MSU-MR-2.png").symlink_to(outside)
    proc.die(0)
    proc.close_pipe()
    assert run.complete.wait(2)
    body = decoder.snapshot()
    assert body["state"] == ("done" if kind.endswith("picture") else "no_picture")
    assert body["passes"] == []
    if kind == "markerless-empty":
        assert body["reason"] == "The close marker was never seen."


def test_meteor_r3_failure_allows_immediate_next_listen(rig):
    decoder, proc, run = _start(rig)
    proc.feed(MARKER, "product processing failed")
    proc.die(7)
    proc.close_pipe()
    assert run.complete.wait(2)
    assert decoder.snapshot()["state"] == "no_picture"
    assert "product processing failed" in decoder.snapshot()["reason"]
    assert decoder.watch.reason is None and "retry_in" not in decoder.snapshot()
    decoder.start()
    assert len(rig[1]) == 2


def test_meteor_finish_command_and_handoff_keep_launch_provenance(rig):
    from sigint101.hardware import RtlSdrDevice

    decoder, proc, run = _start(rig)
    proc.feed(MARKER)
    assert run.physical.wait(2)
    rig[2]["devices"] = [RtlSdrDevice(9, "replacement")]
    decoder.release()
    assert "TERM" not in proc.sent and "KILL" not in proc.sent
    assert decoder.live_command() == shlex.join(proc.args)
    assert decoder.snapshot()["radio"] == {"kind": "rtl-sdr", "index": 4, "serial": "rx"}
    proc.picture()
    proc.die(0)
    proc.close_pipe()
    assert run.complete.wait(2)
    assert decoder.snapshot()["ended_by"] == "self_exit"


def test_meteor_finisher_cleanup_failure_only_refuses_meteor(rig):
    from sigint101.meteor import FINISH_CAP_S, MeteorUnavailable

    rig[2].update(now=50.0, resists_kill=True)
    decoder, proc, run = _start(rig)
    proc.feed(MARKER)
    assert run.physical.wait(2)
    rig[2]["now"] += FINISH_CAP_S
    run.wake.set()
    assert proc.killed.wait(2)
    eventually(lambda: run.reap_failed)
    assert decoder.release_failure(pending=True) is None
    with pytest.raises(MeteorUnavailable, match="finisher"):
        decoder.start()
    assert run.failure == "SatDump finisher survived kill"
    proc.die(-9)
    proc.close_pipe()
    assert run.complete.wait(4)


def test_meteor_shutdown_retries_a_surviving_finisher_without_term(rig, monkeypatch):
    from sigint101.meteor import FINISH_CAP_S

    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 5)
    rig[2].update(now=50.0, resists_kill=True)
    decoder, proc, run = _start(rig)
    proc.feed(MARKER)
    assert run.physical.wait(2)
    rig[2]["now"] += FINISH_CAP_S
    run.wake.set()
    assert proc.killed.wait(2)
    eventually(lambda: run.reap_failed)
    assert not run.reaped
    proc.resists_kill = False
    decoder.shutdown()
    assert run.reaped and run.complete.is_set()
    assert proc.sent.count("KILL") == 2 and "TERM" not in proc.sent


@pytest.mark.parametrize("outcome", [
    "reaped", "already-exited", "timeout", "wait-error", "signal-error", "poll-error",
])
def test_meteor_wait_error_checks_liveness_and_retries_kill_within_budget(
        rig, monkeypatch, outcome):
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 30)
    rig[2]["resists_kill"] = True
    decoder, proc, run = _start(rig)
    waits = []
    kill, poll = proc.kill, proc.poll

    def signal():
        if outcome == "signal-error":
            proc.sent.append("KILL")
            raise OSError("kill failed")
        kill()

    def status():
        if outcome == "poll-error" and waits:
            raise OSError("status failed")
        return poll()

    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            if outcome == "already-exited":
                proc.die(-9)
                proc.close_pipe()
            raise OSError("wait failed")
        if outcome == "wait-error":
            raise OSError("wait failed again")
        if outcome == "reaped":
            proc.die(-9)
            proc.close_pipe()
            return -9
        raise subprocess.TimeoutExpired(proc.args, timeout)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(proc, "kill", signal)
            patch.setattr(proc, "poll", status)
            patch.setattr(proc, "wait", wait)
            decoder.release()
            eventually(lambda: run.reaped or run.reap_failed)
            retried = outcome in ("reaped", "timeout", "wait-error")
            assert proc.sent == (["TERM", "KILL", "KILL"] if retried else ["TERM", "KILL"])
            if retried:
                assert len(waits) == 2 and 0 <= waits[1] <= waits[0]
            if outcome in ("reaped", "already-exited"):
                assert run.complete.wait(2) and run.physical.is_set()
            else:
                assert not run.physical.is_set() and not run.reaped
                if outcome == "timeout":
                    assert decoder.release_failure(pending=True) == (
                        "SatDump survived kill; the dongle may still be held"
                    )
                else:
                    detail = {
                        "wait-error": "wait failed again",
                        "signal-error": "kill failed",
                        "poll-error": "status failed",
                    }[outcome]
                    assert decoder.release_failure(pending=True) == (
                        f"SatDump could not be confirmed stopped; the dongle may still be held: "
                        f"{detail}"
                    )
    finally:
        proc.die(-9)
        proc.close_pipe()


@pytest.mark.parametrize("control", ["stop", "release", "retune"])
def test_meteor_later_control_runs_the_pending_retry_at_once(rig, monkeypatch, control):
    from sigint101.meteor import FINISH_CAP_S

    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 30)
    rig[2].update(now=50.0, resists_kill=True)
    decoder, proc, run = _start(rig)
    proc.feed(MARKER)
    assert run.physical.wait(2)
    rig[2]["now"] += FINISH_CAP_S
    run.wake.set()
    eventually(lambda: run.reap_failed)
    proc.resists_kill = False
    if control == "retune":
        decoder.retune(137_100_000)
    else:
        getattr(decoder, control)()
    assert run.complete.wait(2) and run.reaped
    assert proc.sent.count("KILL") == 2 and "TERM" not in proc.sent
