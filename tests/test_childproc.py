# ABOUTME: Tests the shared radio-child helpers: own session, dies with the console, kill path.
# ABOUTME: Real short-lived processes prove the OS behaviour; fakes at the boundary pin the reasons.

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from sigint101 import childproc
from sigint101.childproc import (
    ChildWatch,
    Pipeline,
    command_line,
    exit_reason,
    spawn,
    stop_process,
)

REPO = Path(__file__).resolve().parent.parent


def _alive(pid: int) -> bool:
    """True while `pid` is a live (not zombie) process."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state != "Z"


def _wait_dead(pid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _reap(proc):
    """Leave no child behind, whatever the test did."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged host
        pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


# --- spawn: own session, alive across request threads, dead with the console ------------------


def test_spawn_puts_the_child_in_its_own_session():
    proc = spawn(["sleep", "5"], stdout=subprocess.DEVNULL)
    try:
        # A session of its own is what lets stop() signal the tool and anything it forked at once.
        assert os.getsid(proc.pid) == proc.pid
        assert os.getpgid(proc.pid) == proc.pid
    finally:
        _reap(proc)


def test_a_child_outlives_the_request_thread_that_started_it():
    # The parent-death signal fires when the *thread* that forked the child exits, not the process.
    # Request threads come and go, so a child started from one must not die when it idles out.
    box = {}
    worker = threading.Thread(
        target=lambda: box.setdefault("proc", spawn(["sleep", "5"], stdout=subprocess.DEVNULL)))
    worker.start()
    worker.join()
    proc = box["proc"]
    try:
        time.sleep(0.3)
        assert proc.poll() is None
    finally:
        _reap(proc)


def test_a_child_dies_with_the_console(tmp_path):
    # kill -9 of the console must not orphan a tool that holds the dongle.
    script = textwrap.dedent("""
        import subprocess, sys, time
        from sigint101.childproc import spawn
        proc = spawn(["sleep", "30"], stdout=subprocess.DEVNULL)
        print(proc.pid, flush=True)
        time.sleep(30)
    """)
    console = subprocess.Popen([sys.executable, "-c", script], cwd=REPO,
                               stdout=subprocess.PIPE, text=True)
    try:
        child = int(console.stdout.readline())
        assert _alive(child)
        console.kill()
        console.wait(timeout=5)
        assert _wait_dead(child), "the tool outlived the console that started it"
    finally:
        console.stdout.close()
        if console.poll() is None:  # pragma: no cover - only if the assertion path raised early
            console.kill()
            console.wait(timeout=5)


# --- stop_process: terminate, wait, kill, wait ------------------------------------------------


def test_stop_kills_a_tool_that_ignores_sigterm_and_its_children(monkeypatch):
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.3)
    proc = spawn(["bash", "-c", "trap '' TERM; sleep 30 & echo $!; wait"],
                 stdout=subprocess.PIPE, text=True)
    try:
        grandchild = int(proc.stdout.readline())
        assert stop_process(proc) is True
        assert proc.poll() == -signal.SIGKILL
        # The whole session went, not just the leader: nothing it forked is left on the dongle.
        assert _wait_dead(grandchild)
    finally:
        _reap(proc)


class _Unkillable:
    """A child in uninterruptible sleep: it never reaps, whatever it is sent."""

    def __init__(self):
        self.sent = []

    def terminate(self):
        self.sent.append("TERM")

    def kill(self):
        self.sent.append("KILL")

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("x", timeout)


def test_stop_reports_a_tool_that_survives_kill(monkeypatch):
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    proc = _Unkillable()
    assert stop_process(proc) is False
    assert proc.sent == ["TERM", "KILL"]


class _Polite:
    def __init__(self):
        self.sent = []

    def terminate(self):
        self.sent.append("TERM")

    def kill(self):  # pragma: no cover - a polite child never needs it
        self.sent.append("KILL")

    def wait(self, timeout=None):
        return 0


def test_stop_does_not_kill_a_tool_that_obeys_sigterm():
    proc = _Polite()
    assert stop_process(proc) is True
    assert proc.sent == ["TERM"]


# --- exit reasons ----------------------------------------------------------------------------


class _Ended:
    """A finished child at the process boundary: its argv, exit status and what it said."""

    def __init__(self, args, code, said=""):
        self.args = args
        self._code = code
        self.stdout = None                  # a Popen without stdout=PIPE carries None here
        self.stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.stderr.write(said)
        self.stderr.flush()

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        if self._code is None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self._code

    def terminate(self):
        pass

    def kill(self):
        pass


RTL_FM_BUSY = ("Found 1 device(s):\n  0:  Realtek, RTL2838UHIDIR, SN: 00000001\n\n"
               "Using device 0: Generic RTL2832U OEM\nusb_claim_interface error -6\n"
               "Failed to open rtlsdr device #0.\n")


def test_a_nonzero_exit_names_the_tool_its_status_and_what_it_said():
    proc = _Ended(["/usr/bin/rtl_433", "-f", "433920000"], 1, RTL_FM_BUSY)
    reason = exit_reason(proc)
    assert reason.startswith("rtl_433 stopped (exit status 1)")
    assert "usb_claim_interface error -6" in reason
    assert "Failed to open rtlsdr device #0." in reason


def test_a_signal_death_names_the_signal():
    proc = _Ended(["multimon-ng", "-a", "AFSK1200"], -signal.SIGKILL)
    assert exit_reason(proc) == "multimon-ng stopped (killed by SIGKILL)"


def test_a_streaming_tool_that_exits_cleanly_is_still_stopped():
    proc = _Ended(["rtl_433"], 0)
    assert exit_reason(proc) == "rtl_433 stopped (exited on its own, status 0)"


def test_a_finite_capture_that_exits_cleanly_is_not_a_death():
    proc = _Ended(["satdump", "live"], 0)
    assert exit_reason(proc, finite=True) is None


def test_a_child_still_running_has_no_exit_reason(monkeypatch):
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0.01)
    assert exit_reason(_Ended(["rtl_433"], None)) is None


def test_a_pipeline_blames_the_stage_that_failed_not_the_one_that_saw_eof():
    rtl_fm = _Ended(["rtl_fm", "-f", "144390000"], 1, RTL_FM_BUSY)
    multimon = _Ended(["multimon-ng", "-a", "AFSK1200"], 0)
    reason = exit_reason(Pipeline(rtl_fm, multimon))
    assert reason.startswith("rtl_fm stopped (exit status 1)")
    assert "multimon-ng" not in reason
    assert "usb_claim_interface error -6" in reason


def test_a_pipeline_does_not_blame_the_stage_its_downstream_broke():
    # Killing multimon-ng hands rtl_fm a SIGPIPE on its next write: a consequence, not a cause.
    rtl_fm = _Ended(["rtl_fm"], -signal.SIGPIPE)
    multimon = _Ended(["multimon-ng"], -signal.SIGKILL)
    assert exit_reason(Pipeline(rtl_fm, multimon)) == "multimon-ng stopped (killed by SIGKILL)"


def test_a_finite_pipeline_ending_on_its_own_schedule_is_not_a_death():
    # SSTV's capture: sox trims and exits 0, sstv decodes and exits 0, rtl_fm gets SIGPIPE.
    stages = (_Ended(["rtl_fm"], -signal.SIGPIPE), _Ended(["sox"], 0), _Ended(["sstv"], 0))
    assert exit_reason(Pipeline(*stages, finite=True), finite=True) is None


def test_a_tool_that_reads_its_own_stderr_passes_the_tail_in():
    proc = _Ended(["dsd-fme", "-fa"], 1)
    proc.stderr.close()
    proc.stderr = None
    reason = exit_reason(proc, tail=["rtlsdr_open failed", "exiting"])
    assert reason == "dsd-fme stopped (exit status 1): rtlsdr_open failed | exiting"


def test_the_stderr_tail_is_bounded():
    noisy = "".join(f"line {n}\n" for n in range(500))
    reason = exit_reason(_Ended(["sox"], 2, noisy))
    assert "line 499" in reason and "line 0\n" not in reason
    assert len(reason) < 400


# --- Pipeline ---------------------------------------------------------------------------------


class _Stage:
    def __init__(self, args, code=None):
        self.args = args
        self.code = code
        self.stdout = iter(())
        self.sent = []

    def poll(self):
        return self.code

    def terminate(self):
        self.sent.append("TERM")

    def kill(self):
        self.sent.append("KILL")

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.code


def test_a_streaming_pipeline_is_down_when_any_stage_is_down():
    rtl_fm, multimon = _Stage(["rtl_fm"], code=1), _Stage(["multimon-ng"])
    assert Pipeline(rtl_fm, multimon).poll() == 1


def test_a_finite_pipeline_runs_until_its_last_stage_ends():
    rtl_fm, sox, sstv = _Stage(["rtl_fm"], -13), _Stage(["sox"], 0), _Stage(["sstv"])
    assert Pipeline(rtl_fm, sox, sstv, finite=True).poll() is None
    sstv.code = 0
    assert Pipeline(rtl_fm, sox, sstv, finite=True).poll() == 0


def test_pipeline_wait_reports_a_stage_that_will_not_stop():
    # The old wrappers swallowed TimeoutExpired, which made every kill escalation dead code.
    pipe = Pipeline(_Stage(["rtl_fm"]), _Stage(["multimon-ng"], code=0))
    with pytest.raises(subprocess.TimeoutExpired):
        pipe.wait(timeout=0.01)


def test_pipeline_terminate_and_kill_reach_every_stage():
    stages = (_Stage(["rtl_fm"]), _Stage(["multimon-ng"]))
    pipe = Pipeline(*stages)
    pipe.terminate()
    pipe.kill()
    assert [s.sent for s in stages] == [["TERM", "KILL"], ["TERM", "KILL"]]


def test_stop_escalates_through_a_real_pipeline(monkeypatch):
    # The audit's probe: both halves trap SIGTERM. stop must still leave neither alive.
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.3)
    first = spawn(["bash", "-c", "trap '' TERM; while :; do sleep 0.1; done"],
                  stdout=subprocess.PIPE)
    second = spawn(["bash", "-c", "trap '' TERM; cat >/dev/null"], stdin=first.stdout,
                   stdout=subprocess.PIPE)
    first.stdout.close()
    pipe = Pipeline(first, second)
    try:
        assert stop_process(pipe) is True
        assert first.poll() is not None and second.poll() is not None
    finally:
        _reap(first)
        _reap(second)


# --- the command a learner can type -----------------------------------------------------------


def test_command_line_is_the_argv_shell_quoted():
    proc = _Stage(["tcpdump", "-i", "wlan1", "-w", "-", "type mgt"])
    assert command_line(proc) == "tcpdump -i wlan1 -w - 'type mgt'"


def test_command_line_of_a_pipeline_joins_the_stages_with_a_pipe():
    pipe = Pipeline(_Stage(["rtl_fm", "-f", "144390000"]),
                    _Stage(["multimon-ng", "-a", "AFSK1200"]))
    assert command_line(pipe) == "rtl_fm -f 144390000 | multimon-ng -a AFSK1200"


def test_command_line_of_nothing_is_none():
    assert command_line(None) is None


def test_command_line_reads_the_real_popen_argv():
    proc = spawn(["sleep", "2"], stdout=subprocess.DEVNULL)
    try:
        assert command_line(proc) == "sleep 2"
    finally:
        _reap(proc)


# --- ChildWatch: who stopped it, and when to try again -----------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _watch(**kw):
    clock = _Clock()
    return ChildWatch("rtl_433", clock=clock, **kw), clock


def test_an_unrequested_death_is_recorded_and_blocks_a_respawn():
    watch, clock = _watch()
    proc = _Ended(["rtl_433"], 1, "No supported devices found.\n")
    watch.spawned(proc)
    reason = watch.ended(proc)
    assert reason.startswith("rtl_433 stopped (exit status 1)")
    assert watch.stopped is True
    assert watch.blocked() == reason
    assert watch.retry_in() == pytest.approx(childproc.FIRST_RETRY_S)
    clock.now += childproc.FIRST_RETRY_S
    assert watch.blocked() is None          # the back-off has run out: try again
    assert watch.stopped is True            # ...but until a spawn, it is still stopped


def test_a_deliberate_stop_is_never_a_death():
    watch, _ = _watch()
    proc = _Ended(["rtl_433"], -signal.SIGTERM)
    watch.spawned(proc)
    watch.stopping()
    assert watch.ended(proc) is None
    assert watch.stopped is False and watch.blocked() is None


def test_a_stale_reader_cannot_report_the_previous_child():
    watch, _ = _watch()
    old, current = _Ended(["rtl_433"], 1), _Ended(["rtl_433"], None)
    watch.spawned(old)
    watch.spawned(current)
    assert watch.ended(old) is None


def test_a_death_is_recorded_once_however_many_observers_see_it():
    watch, clock = _watch()
    proc = _Ended(["rtl_433"], 1)
    watch.spawned(proc)
    assert watch.ended(proc) is not None
    assert watch.ended(proc) is None        # the reader and the next poll both look
    assert watch.retry_in() == pytest.approx(childproc.FIRST_RETRY_S)


def test_repeated_quick_deaths_back_off_further_each_time_up_to_a_cap():
    watch, clock = _watch()
    delays = []
    for _ in range(8):
        proc = _Ended(["rtl_433"], 1)
        watch.spawned(proc)
        clock.now += 0.5                    # dies half a second after it starts
        watch.ended(proc)
        delays.append(watch.retry_in())
        clock.now += watch.retry_in()
    assert delays[:4] == [childproc.FIRST_RETRY_S * 2 ** n for n in range(4)]
    assert max(delays) == childproc.MAX_RETRY_S
    assert delays[-1] == childproc.MAX_RETRY_S


def test_a_child_that_ran_a_good_while_starts_its_back_off_afresh():
    watch, clock = _watch()
    for _ in range(3):
        proc = _Ended(["rtl_433"], 1)
        watch.spawned(proc)
        watch.ended(proc)
        clock.now += watch.retry_in()
    proc = _Ended(["rtl_433"], 1)
    watch.spawned(proc)
    clock.now += childproc.STABLE_AFTER_S
    watch.ended(proc)
    assert watch.retry_in() == pytest.approx(childproc.FIRST_RETRY_S)


def test_a_spawn_clears_the_stopped_state():
    watch, clock = _watch()
    proc = _Ended(["rtl_433"], 1)
    watch.spawned(proc)
    watch.ended(proc)
    clock.now += watch.retry_in()
    watch.spawned(_Ended(["rtl_433"], None))
    assert watch.stopped is False
    assert watch.reason is None
