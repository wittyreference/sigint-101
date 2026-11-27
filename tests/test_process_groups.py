# ABOUTME: Tests stopping a Linux process group after the tool that led it has exited.
# ABOUTME: A real forked helper keeps stdout open until the group is signalled and reaped.

import ctypes
import os
import select
import signal
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from types import SimpleNamespace

import pytest

from sigint101 import childproc

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux subreaping")

FORK_AND_EXIT = textwrap.dedent("""
    import os, signal, sys

    ready, notify = os.pipe()
    helper = os.fork()
    if helper == 0:
        os.close(ready)
        if sys.argv[1] == "ignore":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.alarm(15)
        os.write(notify, b"1")
        os.close(notify)
        while True:
            signal.pause()

    os.close(notify)
    assert os.read(ready, 1) == b"1"
    print(helper, flush=True)
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


@pytest.fixture
def forked_group(request, subreaper):
    mode = getattr(request, "param", "accept")
    with childproc.spawn([sys.executable, "-c", FORK_AND_EXIT, mode],
                         stderr=subprocess.DEVNULL, text=True) as tool:
        try:
            assert tool.wait(timeout=3) == 0
            helper = int(tool.stdout.readline())
            assert os.getpgid(helper) == tool.pid
            assert os.getsid(helper) == tool.pid
            with ThreadPoolExecutor(max_workers=1) as executor:
                reaped = executor.submit(os.waitpid, helper, 0)
                try:
                    yield tool, helper, reaped
                finally:
                    if not reaped.done():
                        with suppress(ProcessLookupError):
                            os.kill(helper, signal.SIGKILL)
                    reaped.result(timeout=3)
        finally:
            if tool.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(tool.pid, signal.SIGKILL)
                tool.wait(timeout=3)


@pytest.mark.parametrize(("forked_group", "wrapped", "last_signal"), [
    ("accept", False, signal.SIGTERM),
    ("ignore", True, signal.SIGKILL),
], indirect=["forked_group"], ids=["terminate", "escalate-in-pipeline"])
def test_stop_reaps_the_helper_of_an_exited_leader(
        forked_group, wrapped, last_signal, monkeypatch):
    tool, helper, reaped = forked_group
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.5)
    target = childproc.Pipeline(tool) if wrapped else tool

    assert childproc.stop_process(target) is True
    with pytest.raises(ProcessLookupError):
        os.killpg(tool.pid, 0)
    assert select.select([tool.stdout], [], [], 0)[0], "the helper still holds stdout open"
    assert tool.stdout.read(1) == ""
    waited_pid, status = reaped.result(timeout=1)
    assert waited_pid == helper
    assert os.waitstatus_to_exitcode(status) == -last_signal


def test_stop_reports_a_surviving_group_after_the_leader_is_reaped(forked_group, monkeypatch):
    tool, _, reaped = forked_group
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.05)
    killpg = os.killpg
    wait = tool.wait
    sent = []
    elapsed = 0.0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    def wait_for_leader(timeout):
        advance(timeout / 2)  # The leader wait uses half of each phase's budget.
        return wait(timeout=timeout)

    def cannot_signal(pgid, sig):
        if pgid == tool.pid:
            if sig != 0:
                sent.append(sig)
            raise PermissionError("simulated failure to signal the group")
        return killpg(pgid, sig)

    with monkeypatch.context() as patch:
        patch.setattr(os, "killpg", cannot_signal)
        patch.setattr(tool, "wait", wait_for_leader)
        patch.setattr(childproc, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=advance))
        assert childproc.stop_process(tool) is False
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert elapsed == pytest.approx(2 * childproc.STOP_WAIT_S)
    assert not reaped.done()
