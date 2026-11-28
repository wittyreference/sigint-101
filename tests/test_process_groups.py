# ABOUTME: Tests signaling Linux process groups while their leader still owns the group ID.
# ABOUTME: Uses real forked helpers and checks for pipe EOF and absence of post-reap signals.

import ctypes
import os
import select
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from sigint101 import childproc

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux subreaping")

WAIT_FOR_EXIT = os.WEXITED | os.WNOHANG | os.WNOWAIT

FORK_HELPER = textwrap.dedent("""
    import os, signal, sys

    ready, notify = os.pipe()
    helper = os.fork()
    if helper == 0:
        os.close(ready)
        if sys.argv[1] == "zombie":
            os._exit(0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.alarm(15)
        os.write(notify, b"1")
        os.close(notify)
        while True:
            signal.pause()

    os.close(notify)
    if sys.argv[1] == "zombie":
        os.waitid(os.P_PID, helper, os.WEXITED | os.WNOWAIT)
    else:
        assert os.read(ready, 1) == b"1"
    os.close(ready)
    print(helper, flush=True)
    sys.stdin.read(1)
""")


@pytest.fixture
def subreaper():
    # Adopt and reap our orphan so the test does not depend on how the host's PID 1 reaps.
    get_subreaper, set_subreaper = 37, 36  # PR_GET_CHILD_SUBREAPER, PR_SET_CHILD_SUBREAPER
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    previous = ctypes.c_int()
    assert prctl(get_subreaper, ctypes.byref(previous), 0, 0, 0) == 0
    assert prctl(set_subreaper, 1, 0, 0, 0) == 0
    try:
        yield
    finally:
        assert prctl(set_subreaper, previous.value, 0, 0, 0) == 0


def _line_from(tool):
    assert select.select([tool.stdout], [], [], 3)[0], "the tool did not announce readiness"
    return tool.stdout.readline()


@pytest.fixture
def forked_group(request, subreaper):
    mode = getattr(request, "param", "live")
    with childproc.spawn([sys.executable, "-c", FORK_HELPER, mode], stdin=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True) as tool:
        helper = None
        try:
            helper = int(_line_from(tool))
            assert os.getpgid(helper) == tool.pid
            assert os.getsid(helper) == tool.pid
            yield tool, helper
        finally:
            if tool.returncode is None:
                tool.kill()
                tool.wait(timeout=3)
            if helper is not None:
                # The tool may have left the helper alive on a failing version of the code.
                try:
                    exited = os.waitid(os.P_PID, helper, WAIT_FOR_EXIT)
                except ChildProcessError:
                    pass  # The test reaped it.
                else:
                    if exited is None:
                        os.kill(helper, signal.SIGKILL)
                    os.waitpid(helper, 0)


@pytest.mark.parametrize("reap", ["poll", "wait", "stdlib", "external"])
def test_a_reaped_tool_never_signals_its_old_group(reap, monkeypatch):
    with childproc.spawn([sys.executable, "-c", "pass"], stderr=subprocess.DEVNULL) as tool:
        if reap == "poll":
            code = _poll_until_exit(tool.poll)
        elif reap == "wait":
            code = tool.wait(timeout=3)
        elif reap == "stdlib":
            # The stdlib's cleanup uses this same internal poll to reap children.
            code = _poll_until_exit(lambda: subprocess.Popen.poll(tool))
        else:
            pid, status = os.waitpid(tool.pid, 0)
            assert pid == tool.pid
            code = os.waitstatus_to_exitcode(status)
            assert tool.returncode is None
        assert code == 0

        calls = []
        with monkeypatch.context() as patch:
            # Observe attempted signals without sending to the now-unowned numeric group ID.
            patch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
            patch.setattr(childproc, "STOP_WAIT_S", 0.01)
            tool.terminate()
            tool.kill()
            stopped = childproc.stop_process(tool)
        assert calls == []
        assert stopped is True


def _poll_until_exit(poll):
    deadline = time.monotonic() + 3
    code = poll()
    while code is None and time.monotonic() < deadline:
        time.sleep(0.005)
        code = poll()
    return code


@pytest.mark.parametrize(("script", "expected"), [
    pytest.param("raise SystemExit(23)", 23, id="exit-status"),
    pytest.param("import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                 -signal.SIGTERM, id="signal"),
])
def test_poll_keeps_the_observed_status_if_another_waiter_reaps_first(
        script, expected, monkeypatch):
    with childproc.spawn([sys.executable, "-c", script], stderr=subprocess.DEVNULL) as tool:
        real_waitpid = os.waitpid
        reaped_codes = []

        def waitpid(pid, options):
            if pid == tool.pid:
                # Poll has observed the exit and swept the group. Reap the leader from
                # outside Popen before the managed waitpid call reaches the kernel.
                reaped_pid, status = real_waitpid(pid, 0)
                assert reaped_pid == pid
                reaped_codes.append(os.waitstatus_to_exitcode(status))
            return real_waitpid(pid, options)

        with monkeypatch.context() as patch:
            patch.setattr(os, "waitpid", waitpid)
            code = _poll_until_exit(tool.poll)
        assert reaped_codes == [expected]
        assert code == tool.returncode == expected


@pytest.mark.parametrize("reap", ["poll", "wait"])
def test_natural_exit_sweeps_the_helper_before_reaping_and_stdout_reaches_eof(
        forked_group, monkeypatch, reap):
    tool, helper = forked_group
    real_killpg = os.killpg
    signals = []

    def signal_group(pgid, sig):
        if pgid == tool.pid:
            # WNOWAIT sees the exit but still leaves the group ID reserved.
            exited = os.waitid(os.P_PID, tool.pid, WAIT_FOR_EXIT)
            signals.append((sig, None if exited is None else exited.si_status))
        real_killpg(pgid, sig)

    with monkeypatch.context() as patch:
        patch.setattr(os, "killpg", signal_group)
        tool.stdin.write("x")
        tool.stdin.flush()
        code = _poll_until_exit(tool.poll) if reap == "poll" else tool.wait(timeout=3)
    assert code == 0
    readable = select.select([tool.stdout], [], [], 1)[0]
    assert readable, "the helper still holds stdout open after its leader was reaped"
    assert tool.stdout.read(1) == ""
    assert signals == [(signal.SIGKILL, 0)]
    assert os.waitstatus_to_exitcode(os.waitpid(helper, 0)[1]) == -signal.SIGKILL


def test_pipeline_escalation_never_signals_a_reaped_sibling(monkeypatch):
    ignores_term = (
        "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); signal.pause()"
    )
    with childproc.spawn([sys.executable, "-c", ignores_term], stderr=subprocess.DEVNULL,
                         text=True) as first:
        try:
            assert _line_from(first) == "ready\n"
            with childproc.spawn([sys.executable, "-c", "pass"], stdin=first.stdout,
                                 stderr=subprocess.DEVNULL) as sibling:
                first.stdout.close()
                assert sibling.wait(timeout=3) == 0
                real_killpg = os.killpg
                calls = []

                def signal_group(pgid, sig):
                    calls.append((pgid, sig))
                    if pgid != sibling.pid:
                        real_killpg(pgid, sig)
                    elif sig == 0:
                        raise ProcessLookupError("the sibling's group has gone")

                with monkeypatch.context() as patch:
                    patch.setattr(childproc, "STOP_WAIT_S", 0.1)
                    patch.setattr(os, "killpg", signal_group)
                    assert childproc.stop_process(childproc.Pipeline(first, sibling)) is True
                assert first.returncode == -signal.SIGKILL
                assert (first.pid, signal.SIGTERM) in calls
                assert (first.pid, signal.SIGKILL) in calls
                assert all(pgid != sibling.pid for pgid, _ in calls)
        finally:
            if first.returncode is None:
                first.kill()
                first.wait(timeout=3)


@pytest.mark.parametrize("forked_group", ["zombie"], indirect=True)
def test_a_zombie_helper_does_not_stall_stop(forked_group, monkeypatch):
    tool, helper = forked_group
    real_killpg = os.killpg
    signals = []

    def signal_group(pgid, sig):
        if pgid == tool.pid:
            signals.append(sig)
        real_killpg(pgid, sig)

    with monkeypatch.context() as patch:
        patch.setattr(childproc, "STOP_WAIT_S", 0.5)
        patch.setattr(os, "killpg", signal_group)
        started = time.monotonic()
        assert childproc.stop_process(tool) is True
        assert time.monotonic() - started < 1
    # Still unreaped, but it could not hold the dongle or stdout after exiting.
    exited = os.waitid(os.P_PID, helper, WAIT_FOR_EXIT)
    assert exited is not None and exited.si_status == 0
    assert 0 not in signals
    assert tool.stdout.read(1) == ""


def test_poll_cannot_reap_a_leader_while_its_group_is_being_signaled(monkeypatch):
    signal_entered = threading.Event()
    finish_signal = threading.Event()
    with childproc.spawn([sys.executable, "-c", "import sys; sys.stdin.read(1)"],
                         stdin=subprocess.PIPE, stderr=subprocess.DEVNULL) as tool:
        real_killpg = os.killpg

        def signal_group(pgid, sig):
            if pgid == tool.pid and sig == signal.SIGTERM:
                signal_entered.set()
                finish_signal.wait(5)
            else:
                real_killpg(pgid, sig)

        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", signal_group)
            sending = threading.Thread(target=tool.terminate)
            sending.start()
            try:
                assert signal_entered.wait(3)
                tool.stdin.write(b"x")
                tool.stdin.flush()
                assert _poll_until_exit(lambda: os.waitid(
                    os.P_PID, tool.pid, WAIT_FOR_EXIT)) is not None
                assert tool.poll() is None, "poll reaped the leader during the group signal"
            finally:
                finish_signal.set()
                sending.join(3)
        assert not sending.is_alive()
        assert tool.wait(timeout=3) == 0


def test_group_priority_changes_only_a_live_owned_leader(forked_group, monkeypatch):
    tool, helper = forked_group
    previous = os.getpriority(os.PRIO_PROCESS, tool.pid)
    helper_priority = os.getpriority(os.PRIO_PROCESS, helper)
    target = min(previous + 1, 19)
    changed = []
    setpriority = os.setpriority

    def record(which, pid, nice):
        changed.append((pid, nice, tool._waitpid_lock.locked()))
        setpriority(which, pid, nice)

    monkeypatch.setattr(os, "setpriority", record)
    # An inherited nice value of 19 already meets the target.
    assert tool.lower_priority(target) is (target > previous)
    assert not tool.lower_priority(previous)
    assert os.getpriority(os.PRIO_PROCESS, tool.pid) == target
    assert os.getpriority(os.PRIO_PROCESS, helper) == helper_priority
    assert changed == ([(tool.pid, target, True)] if target > previous else [])
    tool.stdin.write("x")
    tool.stdin.flush()
    assert select.select([tool.stdout], [], [], 3)[0] or tool.poll() is not None
    tool.wait(timeout=3)
    assert not tool.lower_priority(19)
    assert len(changed) == (1 if target > previous else 0)
