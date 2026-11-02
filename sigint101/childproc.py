# ABOUTME: Spawns, stops and diagnoses the console's radio tools (rtl_fm, rtl_433, dump1090, ...).
# ABOUTME: Each tool gets its own session, dies with the console, is killed if it ignores SIGTERM.

from __future__ import annotations

import ctypes
import os
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

# How long stop() waits for a tool to exit after asking (SIGTERM), and again after insisting
# (SIGKILL). rtl_* tools release the USB interface in well under a second when they obey.
STOP_WAIT_S = 3.0

# How long to wait for a tool's exit status once its output has ended. Output closes as the
# process exits, so the status is usually there at once; the grace covers the scheduler.
EXIT_GRACE_S = 2.0

# Respawn back-off after a tool dies on its own: the first retry waits this long, each further
# quick death doubles it, up to the cap. A child that ran at least STABLE_AFTER_S before dying
# was working, so its back-off starts afresh.
FIRST_RETRY_S = 2.0
MAX_RETRY_S = 30.0
STABLE_AFTER_S = 30.0

# How much of a tool's stderr goes into the reason the panel shows: its last few lines, and no
# more than this many characters, so a chatty tool cannot bury the line that says why.
TAIL_LINES = 3
TAIL_CHARS = 300
TAIL_READ_BYTES = 4096

_PR_SET_PDEATHSIG = 1


def _libc_prctl():
    """The C library's prctl, or None where there is none (anything but Linux)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):  # pragma: no cover - a Linux without a usable libc
        return None


# Resolved once, in the parent, so the child only makes a call — no library loading after fork.
_PRCTL = _libc_prctl()


def _die_with_parent():
    """A preexec hook that SIGKILLs the child when the thread that forked it exits.

    SIGKILL rather than SIGTERM, because the case this exists for is a tool that has stopped
    listening: the audit caught an rtl_fm that caught SIGTERM and sat in a futex for 98 s, holding
    the dongle. The parent pid is captured before the fork, so a console that died in the gap
    between fork and prctl is caught too.
    """
    if _PRCTL is None:
        return None
    parent = os.getpid()

    def hook():
        _PRCTL(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != parent:
            os._exit(1)

    return hook


class _Spawner:
    """One long-lived thread that runs every Popen for the console.

    The kernel delivers the parent-death signal when the *thread* that forked the child exits, not
    when the process does. Request threads are pooled and expire when idle, so a tool forked from
    one would be killed seconds into a lesson. Forking from this thread, which lives exactly as long
    as the console, makes "the parent died" mean what it says.
    """

    def __init__(self):
        self._jobs = queue.SimpleQueue()
        self._thread = None
        self._lock = threading.Lock()

    def run(self, fn):
        if threading.current_thread() is self._thread:
            return fn()
        self._ensure_thread()
        done = threading.Event()
        box = {}

        def job():
            try:
                box["value"] = fn()
            except BaseException as exc:  # handed back to the caller, which re-raises it
                box["error"] = exc
            finally:
                done.set()

        self._jobs.put(job)
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="sigint101-spawner",
                                                 daemon=True)
                self._thread.start()

    def _loop(self):
        while True:
            self._jobs.get()()


_SPAWNER = _Spawner()


class GroupPopen(subprocess.Popen):
    """A Popen whose terminate() and kill() reach the tool's whole session, not just its leader.

    A tool that forks (a shell wrapper, a helper process) would otherwise leave a child holding the
    dongle after its parent was stopped.
    """

    def send_signal(self, sig):
        self.poll()
        if self.returncode is not None:
            return
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:  # the session is already gone, which is what was wanted
            pass


#: Pass as `stderr=` to keep the tool's last words in a scratch file for the exit reason.
CAPTURE = object()


def spawn(argv, *, stdin=None, stdout=subprocess.PIPE, stderr=CAPTURE, text=False):
    """Start one radio tool in its own session, tied to the console's life.

    stderr defaults to a scratch file rather than a pipe: nothing drains stderr while the tool
    runs, and a full pipe would wedge the tool rather than the diagnostic. The file is attached as
    `proc.stderr`, so the reason a tool died is in one place to read.
    """
    scratch = None
    if stderr is CAPTURE:
        scratch = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        stderr = scratch
    try:
        proc = _SPAWNER.run(lambda: GroupPopen(
            argv, stdin=stdin, stdout=stdout, stderr=stderr, text=text,
            start_new_session=True, preexec_fn=_die_with_parent()))
    except BaseException:
        if scratch is not None:
            scratch.close()
        raise
    if scratch is not None:
        proc.stderr = scratch
    return proc


class Pipeline:
    """Several tools piped stdout to stdin, run and reaped as one object.

    The reader reads the last stage's stdout. A streaming pipeline (rtl_fm | multimon-ng) is down
    the moment any stage is down: a dead rtl_fm behind a live multimon-ng decodes nothing, and
    polling only the last stage is how the console used to call that LIVE. A finite capture
    (rtl_fm | sox | sstv) ends by design when its last stage finishes, so it runs until then.
    """

    def __init__(self, *procs, finite=False):
        self.procs = procs
        self.finite = finite
        self.stdout = procs[-1].stdout

    @property
    def argvs(self) -> list:
        return [proc.args for proc in self.procs]

    def poll(self):
        if self.finite:
            return self.procs[-1].poll()
        codes = [proc.poll() for proc in self.procs]
        ended = [code for code in codes if code is not None]
        if not ended:
            return None
        causes = [code for code in ended if code not in (0, -signal.SIGPIPE)]
        return (causes or ended)[0]

    def terminate(self):
        # Last stage first, so an upstream tool never blocks writing into a reader already gone.
        for proc in reversed(self.procs):
            _quietly(proc.terminate)

    def kill(self):
        for proc in reversed(self.procs):
            _quietly(proc.kill)

    def wait(self, timeout=None):
        """Wait for every stage. Raises TimeoutExpired if any is still running at the deadline."""
        deadline = None if timeout is None else time.monotonic() + timeout
        for proc in self.procs:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            proc.wait(timeout=left)
        return self.poll()


def _quietly(signal_call):
    try:
        signal_call()
    except OSError:  # the process is already gone
        pass


def stop_process(proc) -> bool:
    """Ask a tool to stop, insist if it ignores the ask, and confirm the OS has reaped it.

    Returns False only when even SIGKILL left it unreaped (a process in uninterruptible sleep):
    the caller reports that, because the next tool will find the dongle busy and deserves to know
    why.
    """
    _quietly(proc.terminate)
    try:
        proc.wait(timeout=STOP_WAIT_S)
        return True
    except subprocess.TimeoutExpired:
        pass
    _quietly(proc.kill)
    try:
        proc.wait(timeout=STOP_WAIT_S)
        return True
    except subprocess.TimeoutExpired:
        return False


def _stages(proc) -> tuple:
    procs = getattr(proc, "procs", None)
    return tuple(procs) if procs else (proc,)


def _name(proc, fallback: str) -> str:
    args = getattr(proc, "args", None)
    if args:
        first = args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]
        return os.path.basename(str(first))
    return fallback


def _settled_code(proc, deadline: float):
    """The exit status, waiting until `deadline` for one that is on its way."""
    code = proc.poll()
    if code is None:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        code = proc.poll()
    return code


def _how(code: int) -> str:
    if code > 0:
        return f"exit status {code}"
    if code < 0:
        try:
            return f"killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    return "exited on its own, status 0"


def stderr_tail(proc) -> list:
    """The last few lines a tool wrote to its captured stderr, or [] if none were captured."""
    stream = getattr(proc, "stderr", None)
    if stream is None:
        return []
    try:
        if not stream.seekable():
            return []
        end = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, end - TAIL_READ_BYTES))
        text = stream.read()
    except (OSError, ValueError):  # closed, or never a file
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-TAIL_LINES:]


def _describe(name: str, code: int, tail) -> str:
    said = " | ".join(tail)
    if len(said) > TAIL_CHARS:
        said = "…" + said[-TAIL_CHARS:]
    return f"{name} stopped ({_how(code)})" + (f": {said}" if said else "")


def exit_reason(proc, *, finite=False, tail=None, tool="the decoder") -> str | None:
    """Why a tool (or pipeline) is no longer running, or None if it is running or ended cleanly.

    A streaming decoder never ends by design, so any exit is reported, status 0 included. A
    finite capture (an SSTV recording, a satellite pass) ending with status 0 is its normal end.
    In a pipeline the stage that failed is blamed, not the stages that merely saw it go: a
    downstream tool exiting 0 on end-of-input, or an upstream one killed by SIGPIPE, is a
    consequence. `tail` carries the last lines of a tool whose stderr the decoder reads itself.
    """
    deadline = time.monotonic() + EXIT_GRACE_S
    ended = []
    for stage in _stages(proc):
        code = _settled_code(stage, deadline)
        if code is not None:
            ended.append((stage, code))
    if not ended:
        return None
    causes = [(stage, code) for stage, code in ended if code not in (0, -signal.SIGPIPE)]
    if not causes:
        if finite:
            return None
        causes = ended[:1]
    parts = []
    for stage, code in causes:
        lines = list(tail) if tail is not None else stderr_tail(stage)
        parts.append(_describe(_name(stage, tool), code, lines[-TAIL_LINES:]))
    return "; ".join(parts)


def command_line(proc) -> str | None:
    """The shell line a live process object is running, as a learner could type it.

    Read off the process object itself (Popen keeps the exact argv it executed), never rebuilt
    from settings, so the strip on the panel cannot drift from what actually runs. A pipeline
    reads "a | b".
    """
    if proc is None:
        return None
    argvs = getattr(proc, "argvs", None)
    if argvs is None:
        args = getattr(proc, "args", None)
        argvs = [args] if args else []
    lines = [shlex.join(str(arg) for arg in argv) for argv in argvs if argv]
    return " | ".join(lines) or None


def live_command(proc) -> str | None:
    """The command line of a process that is running right now, else None."""
    if proc is None or proc.poll() is not None:
        return None
    return command_line(proc)


class ChildWatch:
    """One decoder's record of its child: whether we stopped it, why it died, when to retry.

    The reader thread reports the end of the child's output, and so does the next poll that finds
    it gone; the first to look records the death, once. stop() marks the child as ours to end
    first, so a deliberate stop is never reported as a death. Before spawning again, start() asks
    blocked(): a tool that keeps dying waits longer each time instead of respawning on every poll.
    """

    def __init__(self, tool: str = "the decoder", *, finite=False, clock=time.monotonic):
        self.tool = tool
        self.finite = finite
        self._clock = clock
        self._lock = threading.Lock()
        self._current = None
        self._started_at = None
        self._deaths = 0
        self._retry_at = None
        self.reason = None

    def spawned(self, proc) -> None:
        with self._lock:
            self._current = proc
            self._started_at = self._clock()
            self.reason = None

    def stopping(self) -> None:
        with self._lock:
            self._current = None

    def ended(self, proc, *, tail=None) -> str | None:
        """Record the child's death if it was not ours. Returns the reason the first time only."""
        with self._lock:
            if proc is None or proc is not self._current:
                return None
            reason = exit_reason(proc, finite=self.finite, tail=tail, tool=self.tool)
            if reason is None:
                if proc.poll() is not None:
                    self._current = None  # a finite capture's clean end: nothing to report
                return None
            self._current = None
            now = self._clock()
            lived = now - self._started_at if self._started_at is not None else 0.0
            self._deaths = 1 if lived >= STABLE_AFTER_S else self._deaths + 1
            delay = min(FIRST_RETRY_S * 2 ** (self._deaths - 1), MAX_RETRY_S)
            self._retry_at = now + delay
            self.reason = reason
            return reason

    @property
    def stopped(self) -> bool:
        """True from a child's own death until the next spawn."""
        return self.reason is not None

    def retry_in(self) -> float | None:
        """Seconds until the console may try the tool again, or None if it may now."""
        if self.reason is None or self._retry_at is None:
            return None
        left = self._retry_at - self._clock()
        return left if left > 0 else None

    def blocked(self) -> str | None:
        """The death reason while the back-off runs, else None."""
        return self.reason if self.retry_in() is not None else None
