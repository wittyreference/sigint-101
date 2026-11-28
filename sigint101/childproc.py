# ABOUTME: Spawns, stops and diagnoses the console's radio tools (rtl_fm, rtl_433, dump1090, ...).
# ABOUTME: Each tool gets its own session, dies with the console, is killed if it ignores SIGTERM.

from __future__ import annotations

import ctypes
import inspect
import os
import queue
import re
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
# The quick death from which the wait has grown past FIRST_RETRY_S (the second in a row). A retune
# lifts a shorter wait; from this one on, the wait survives the retune (ChildWatch.superseded).
RETUNE_KEEPS_WAIT_FROM = 2

# How long a fresh child is "starting" rather than live. rtl_fm takes a few hundred ms to fail on
# a dongle another process holds, so a tool has not shown it works until it has lived this long.
SETTLE_S = 2.0

# How much of a tool's stderr goes into the reason the panel shows: its last two lines, and no
# more than this many characters, so a chatty tool cannot bury the line that says why. The cap
# does not keep the countdown inside the 124 px panel — at 160 chars, a death plus its countdown
# runs to 4 lines. Lesson panels that opt into .decoder--fits-refusal grow to show it whole; the
# radar and the rack scroll.
TAIL_LINES = 2
TAIL_CHARS = 160
TAIL_READ_BYTES = 4096

# A tail line that reports a failure rather than narrating start-up. When the tail is longer than
# TAIL_CHARS, only such lines are kept, so the cap cannot cut the cause away.
_ERROR_LINE = re.compile(r"error|fail|unable|cannot|can't|usb_claim_interface", re.IGNORECASE)

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
    """Signal the tool's group while its leader still holds the group ID.

    A leader that exits is kept unreaped until SIGKILL has reached any helpers in its group.
    The child then keeps its own exit status, and its PID is safe to release.
    """

    def _exit_pending(self) -> os.waitid_result | None:
        """Peek at the leader under Popen's waitpid lock, leaving its pid reserved."""
        try:
            return os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            # Match Popen's status when the child was already reaped elsewhere.
            self.returncode = 0
            return None

    def poll(self):
        # Use the same lock as Popen's internal poll (including stdlib cleanup) and
        # send_signal. No managed reap can release the pid midway through a signal.
        if self.returncode is None and self._waitpid_lock.acquire(False):
            try:
                if self.returncode is None and (exited := self._exit_pending()) is not None:
                    _quietly(lambda: self._signal_group(signal.SIGKILL))
                    try:
                        _, status = os.waitpid(self.pid, 0)  # waitid confirmed the exit
                    except ChildProcessError:
                        # Another waiter reaped the leader after waitid observed its exit.
                        self.returncode = (exited.si_status if exited.si_code == os.CLD_EXITED
                                           else -exited.si_status)
                    else:
                        self._handle_exitstatus(status)
            finally:
                self._waitpid_lock.release()
        return self.returncode

    def _wait(self, timeout):
        # Popen.wait calls this hook. Sleep outside the lock so a parallel stop can
        # still send its signal while the leader is running.
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = 0.0005
        while (code := self.poll()) is None:
            delay = min(2 * delay, 0.05)
            if deadline is None:
                time.sleep(delay)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                time.sleep(min(delay, remaining))
        return code

    def send_signal(self, sig):
        with self._waitpid_lock:
            if self.returncode is None:
                self._exit_pending()  # ECHILD means there is no longer an owned leader.
                if self.returncode is None:
                    self._signal_group(sig)

    def lower_priority(self, nice: int) -> bool:
        """Lower only a live, owned leader's priority while managed reaping is excluded."""
        with self._waitpid_lock:
            if self.returncode is not None or self._exit_pending() is not None:
                return False
            if self.returncode is not None:
                return False  # waitid reported that the leader was reaped elsewhere
            try:
                if os.getpriority(os.PRIO_PROCESS, self.pid) >= nice:
                    return False
                os.setpriority(os.PRIO_PROCESS, self.pid, nice)
            except ProcessLookupError:
                return False
            return True

    def _signal_group(self, sig):
        """Signal with the waitpid lock held and the leader still owned."""
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            pass


#: Pass as `stderr=` to keep the tool's last words in a scratch file for the exit reason.
CAPTURE = object()


def spawn(argv, *, stdin=None, stdout=subprocess.PIPE, stderr=CAPTURE, text=False, env=None,
          pass_fds=()):
    """Start one radio tool in its own session, tied to the console's life.

    stderr defaults to a scratch file rather than a pipe: nothing drains stderr while the tool
    runs, and a full pipe would wedge the tool rather than the diagnostic. The file is attached as
    `proc.stderr`, so the reason a tool died is in one place to read.
    SSTV and Meteor use `pass_fds` to keep their staging locks held by the child.
    """
    scratch = None
    if stderr is CAPTURE:
        scratch = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        stderr = scratch
    try:
        proc = _SPAWNER.run(lambda: GroupPopen(
            argv, stdin=stdin, stdout=stdout, stderr=stderr, text=text, env=env,
            pass_fds=pass_fds, start_new_session=True, preexec_fn=_die_with_parent()))
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
    """Ask the tool to stop, insist if necessary, and wait for its leaders to be reaped.

    GroupPopen sweeps the leader's group before reaping it. Only a leader that
    survives the SIGKILL wait leaves the stop unconfirmed.
    """
    _quietly(proc.terminate)
    if _wait_stopped(proc, STOP_WAIT_S):
        return True
    _quietly(proc.kill)
    return _wait_stopped(proc, STOP_WAIT_S)


def _wait_stopped(proc, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


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
    try:
        return read_stderr_tail(proc)
    except (OSError, ValueError):  # closed, or never a file
        return []


def read_stderr_tail(proc) -> list:
    """Read captured stderr, propagating a fault in the stream."""
    stream = getattr(proc, "stderr", None)
    if stream is None or not stream.seekable():
        return []
    end = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, end - TAIL_READ_BYTES))
    text = stream.read()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-TAIL_LINES:]


def _describe(name: str, code: int, tail) -> str:
    said = " | ".join(tail)
    if len(said) > TAIL_CHARS:
        # Too long to keep whole, so the lines that say what went wrong go first. rtl_433's
        # protocol-registration narration is longer than the cap by itself, and cutting from the
        # front dropped the usb_claim_interface error that sat beside it.
        errors = [line for line in tail if _ERROR_LINE.search(line)]
        if errors:
            said = " | ".join(errors)
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
    return describe_exits(ended, finite=finite, tail=tail, tool=tool)


def describe_exits(ended, *, finite=False, tail=None, tool="the decoder", read_tail=None):
    """Describe a snapshot without waiting on a child."""
    if not ended:
        return None
    causes = [(stage, code) for stage, code in ended if code not in (0, -signal.SIGPIPE)]
    if not causes:
        if finite:
            return None
        causes = ended[:1]
    parts = []
    for stage, code in causes:
        # A stage that is no process (Flock's serial link to the Marauder devboard) has no exit
        # status, so it ends in its own words rather than with an invented one.
        own = getattr(stage, "end_reason", None)
        if own:
            parts.append(own)
            continue
        # A tool killed by a signal did not get to say why; its last lines are start-up
        # narration, so only a tool that exited on its own has its words quoted.
        if code < 0:
            lines = []
        else:
            lines = list(tail) if tail is not None else (read_tail or stderr_tail)(stage)
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


def running_command(proc) -> str | None:
    """The command line of a process that is running right now, else None."""
    if proc is None or proc.poll() is not None:
        return None
    return command_line(proc)


def _provenance_keywords(hook) -> set[str]:
    """Provenance arguments this hook accepts by keyword."""
    try:
        params = inspect.signature(hook).parameters
    except (TypeError, ValueError):
        return set()
    names = {"command", "freq_hz", "radio"}
    kw_kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    accepted = {name for name in names if name in params and params[name].kind in kw_kinds}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        # Existing wrappers can forward **kwargs to hooks that only take command and freq_hz.
        accepted.update(("command", "freq_hz"))
    return accepted


def emit_reception(hook, item: dict, *, command: str | None = None, freq_hz=None,
                   radio: dict | None = None) -> None:
    """Call `hook` with `item` and the run provenance its signature supports.

    The console can log the command, frequency and radio even if the reader stalls after
    admitting an item and a replacement run starts. Hooks that accept command and frequency
    still receive them together. A named radio keyword opts in to the radio snapshot.
    """
    accepted = _provenance_keywords(hook)
    kwargs = {}
    if {"command", "freq_hz"} <= accepted:
        kwargs.update(command=command, freq_hz=freq_hz)
    if radio is not None and "radio" in accepted:
        kwargs["radio"] = radio.copy()
    hook(item, **kwargs)


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
        self.clock = clock
        self._lock = threading.Lock()
        self._current = None
        self._started_at = None
        self._deaths = 0
        self._retry_at = None
        self._death = None
        self.reason = None

    def spawned(self, proc) -> None:
        with self._lock:
            self._current = proc
            self._started_at = self.clock()
            self._death = None
            self.reason = None

    def stopping(self) -> None:
        with self._lock:
            self._current = None

    def ended(self, proc, *, tail=None, runs: RunGuard | None = None,
              run: int | None = None) -> str | None:
        """Record the child's death if it was not ours. Returns the reason the first time only.

        Exit diagnostics (which can wait for the process's exit status) are computed outside both
        the watch lock and any RunGuard lock. When `runs` is supplied, committing a death and its
        retry deadline requires both that `run` is still current under `runs.lock` and that `proc`
        is still the current child.
        """
        if proc is None:
            return None
        with self._lock:
            if proc is not self._current:
                return None
        reason = exit_reason(proc, finite=self.finite, tail=tail, tool=self.tool)
        if runs is not None:
            with runs:
                if not runs.is_current(run):
                    return None
                return self._commit_end(proc, reason)
        return self._commit_end(proc, reason)

    def observed(self, proc, reason: str | None, observed_at: float):
        """Record an already observed exit without waiting or reading the diagnostics."""
        return self._commit_end(proc, reason, observed_at)

    def superseded(self, freq_hz: int) -> None:
        """A retune moved on to `freq_hz`: the last death is the old run's, not the next one's.

        A single death's wait is lifted, so one kill and a retune start at once. From the second
        quick death in a row the wait has grown (each further quick death doubles FIRST_RETRY_S),
        and while it runs it stands: the tool keeps dying whatever the frequency, and the reason
        says so and where the next try is, not the old death. The count of quick deaths is kept.
        A late diagnostic of the old death finds no death to enrich.
        """
        with self._lock:
            self._death = None
            if (self.reason is not None and self._deaths >= RETUNE_KEEPS_WAIT_FROM
                    and self._retry_at is not None and self._retry_at > self.clock()):
                self.reason = f"{self.tool} keeps dying; it tries again at {freq_hz / 1e6:.4f} MHz"
            else:
                self.reason = None

    def enrich(self, proc, reason: str) -> None:
        """Only the death that is still current can receive its late diagnostic."""
        with self._lock:
            if proc is self._death:
                self.reason = reason

    def _commit_end(self, proc, reason: str | None, observed_at=None) -> str | None:
        with self._lock:
            if proc is not self._current:
                return None
            if reason is None:
                if observed_at is not None or proc.poll() is not None:
                    self._current = None  # a finite capture's clean end: nothing to report
                return None
            self._current = None
            self._death = proc
            now = self.clock() if observed_at is None else observed_at
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

    def settling(self) -> bool:
        """True for a child's first moments, before it has shown it can hold the radio."""
        with self._lock:
            return (self._current is not None and self._started_at is not None
                    and self.clock() - self._started_at < SETTLE_S)

    def retry_in(self) -> float | None:
        """Seconds until the console may try the tool again, or None if it may now."""
        if self.reason is None or self._retry_at is None:
            return None
        left = self._retry_at - self.clock()
        return left if left > 0 else None

    def blocked(self) -> str | None:
        """The death reason while the back-off runs, else None."""
        return self.reason if self.retry_in() is not None else None


class RunGuard:
    """Pairs a decoder's run number with the lock that guards its buffer and error state.

    Every stop ends the current run under that lock before stopping the child, so a reader that
    outlives its stop cannot list a late item, report it, or record a pipe failure or death into
    the next run. Sharing one lock between the run number and the buffer ensures a stop waits for
    any update already past its check, and a check arriving after the stop sees the run has ended.
    """

    def __init__(self, lock=None):
        self.lock = threading.Lock() if lock is None else lock
        self._run = 0

    @property
    def current(self) -> int:
        """The active run number, handed to the reader a start() launches (under _life)."""
        return self._run

    def end(self, watch: ChildWatch | None = None, *, expected: int | None = None) -> int:
        """End the current run under the lock, and mark `watch` stopping in the same step."""
        with self.lock:
            if expected is not None and self._run != expected:
                return self._run
            self._run += 1
            if watch is not None:
                watch.stopping()
            return self._run

    def is_current(self, run: int) -> bool:
        """True while `run` is still the current run. Call with this guard's lock held."""
        return run == self._run

    def __enter__(self):
        self.lock.__enter__()
        return self

    def __exit__(self, *exc):
        return self.lock.__exit__(*exc)
