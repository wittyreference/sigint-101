# ABOUTME: Tests idle retirement, downstream EOF, and cancellation at real process boundaries.
# ABOUTME: Uses gates to keep the final consumer live until each ordering has been checked.

import contextlib
import os
import select
import signal
import subprocess
import sys
import threading
import time

import pytest

from sigint101 import aprs, childproc
from tests.child_fakes import LateProc
from tests.test_stale_readers import APRS_1, APRS_2


def until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return bool(predicate())


def _decoder(proc):
    return aprs.AprsDecoder(spawn=lambda *_: proc, radio_present=lambda: True, which=lambda t: t)


def test_child_exit_retires_reader_without_a_follow_up_request():
    proc = LateProc(["aprs"])
    decoder = _decoder(proc)
    decoder.STOP_WAIT_S = 0.05
    heard = []
    decoder.on_reception = heard.append
    try:
        decoder.start()
        run = decoder._runs.current
        proc.feed(APRS_1)
        assert until(lambda: len(heard) == 1)
        proc.die(7)
        assert not decoder.running
        assert until(lambda: decoder._runs.current != run, seconds=2), (
            "the dead child never retired while no route asked it to start")
        proc.feed(APRS_2)
        proc.break_pipe(OSError("a retired reader's error"))
        assert until(lambda: not proc.reader.is_alive())
        assert [item["source"] for item in heard] == ["KD2ABC-9"]
        assert "status 7" in decoder.watch.reason
        assert decoder.error != "a retired reader's error"
        assert len(decoder.stations()) == 1
    finally:
        proc.close_pipe()
        decoder.stop()


@contextlib.contextmanager
def _pipeline():
    gate_read, gate_write = os.pipe()
    seen_read, seen_write = os.pipe()
    with contextlib.ExitStack() as cleanup:
        for fd in (gate_read, gate_write, seen_read, seen_write):
            cleanup.callback(os.close, fd)
        upstream = childproc.spawn(
            [sys.executable, "-c",
             "import sys; sys.stdin.read(); print(sys.argv[1], flush=True); sys.exit(7)", APRS_1],
            stdin=subprocess.PIPE)
        cleanup.callback(childproc.stop_process, upstream)
        downstream = childproc.GroupPopen(
            [sys.executable, "-c",
             "import os,sys; data=sys.stdin.read(); os.write(int(sys.argv[1]),b'y'); "
             "os.read(int(sys.argv[2]),1); print(data.strip(),flush=True)",
             str(seen_write), str(gate_read)],
            stdin=upstream.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True, pass_fds=(seen_write, gate_read))
        cleanup.callback(childproc.stop_process, downstream)
        upstream.stdout.close()
        decoder = _decoder(childproc.Pipeline(upstream, downstream))
        cleanup.callback(decoder.stop)
        yield decoder, upstream, downstream, seen_read, gate_write


def _reach_eof(decoder, upstream, downstream, seen):
    decoder.start()
    upstream.stdin.close()
    assert select.select([seen], [], [], 4)[0] and os.read(seen, 1) == b"y"
    assert until(lambda: upstream.poll() == 7)
    assert downstream.poll() is None


def _start_again(decoder, entered, finished, result):
    entered.set()
    try:
        decoder.start()
    except aprs.AprsUnavailable as exc:
        result.append(exc)
    finally:
        finished.set()


def test_upstream_exit_preserves_final_packet_in_the_intermediate_pipe(monkeypatch):
    with _pipeline() as (decoder, upstream, downstream, seen, gate):
        calls, heard = [], []
        killpg = os.killpg

        def spy(pgid, sig):
            calls.append((pgid, sig))
            killpg(pgid, sig)

        def receive(item, *, command=None, freq_hz=None, radio=None):
            heard.append((item["source"], command, freq_hz, radio))

        monkeypatch.setattr(os, "killpg", spy)
        decoder.on_reception = receive
        _reach_eof(decoder, upstream, downstream, seen)
        assert calls.count((upstream.pid, signal.SIGKILL)) == 1
        index = len(calls)
        command = childproc.command_line(decoder._proc)
        entered, finished, result = threading.Event(), threading.Event(), []
        start = threading.Thread(target=_start_again, daemon=True,
                                 args=(decoder, entered, finished, result))
        start.start()
        assert entered.wait(2)
        assert finished.wait(5)
        assert result
        survived = downstream.poll() is None
        if survived:
            os.write(gate, b"y")
        assert all(pgid != upstream.pid for pgid, _ in calls[index:])
        assert survived, "the follow-up start killed the consumer before it emitted its packet"
        assert until(lambda: len(heard) == 1)
        assert heard[0][:3] == ("KD2ABC-9", command, aprs.DEFAULT_APRS_HZ)
        assert heard[0][3]["kind"] == "rtl-sdr"
        assert "status 7" in decoder.watch.reason
        assert until(lambda: downstream.poll() == 0)


@pytest.mark.parametrize("action", ["stop", "retune"])
def test_stop_retune_and_handoff_cancel_eof_grace(action):
    with _pipeline() as (decoder, upstream, downstream, seen, _gate):
        _reach_eof(decoder, upstream, downstream, seen)
        entered, finished, result = threading.Event(), threading.Event(), []
        starter = threading.Thread(target=_start_again, daemon=True,
                                   args=(decoder, entered, finished, result))
        starter.start()
        assert entered.wait(2)
        time.sleep(0.3)  # The follow-up sees a real upstream exit on either implementation.
        assert downstream.poll() is None
        before = time.monotonic()
        if action == "stop":
            decoder.stop()
        else:
            decoder.retune(144_800_000)
        elapsed = time.monotonic() - before
        assert finished.wait(5)
        assert downstream.poll() is not None
        assert elapsed < 0.75, f"{action} waited {elapsed:.2f}s behind natural EOF"
