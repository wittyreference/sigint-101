# ABOUTME: Owns teardown and reader retirement for a receiver's exact process or feed.
# ABOUTME: Separates hardware release from reader drain, diagnostics, and report completion.

import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import childproc

SUPERVISOR_POLL_S = 0.1
STREAM_EOF_S = 3.0
PHYSICAL_WAIT_S = 7.0
SAME_TARGET_WAIT_S = 2.0
REAP_RETRY_MIN_S = 2.0
REAP_RETRY_WAIT_S = 0.1
DIRECT_CAPTURE_WAIT_S = 10.0
SHUTDOWN_REPORT_WAIT_S = 6.0
DIAGNOSTIC_WAIT_S = 3.0


class PartialLaunch(RuntimeError):
    """A later stage refused after its predecessor had already taken the radio."""

    def __init__(self, proc, failure):
        super().__init__(str(failure))
        self.proc = proc


def launch_consumer(upstream, spawn, argv):
    try:
        return spawn(argv, stdin=upstream.stdout, stdout=subprocess.PIPE, text=True)
    except (OSError, ValueError) as exc:
        raise PartialLaunch(upstream, exc) from exc
    finally:
        # Let the source see SIGPIPE if the consumer cannot read or goes away.
        if upstream.stdout is not None:
            upstream.stdout.close()


def daemon(target, *args, name):
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


@dataclass(eq=False)
class Run:
    proc: object
    number: int
    watch: childproc.ChildWatch
    args: tuple
    wake: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)
    physical: threading.Event = field(default_factory=threading.Event)
    ready: threading.Event = field(default_factory=threading.Event)
    complete: threading.Event = field(default_factory=threading.Event)
    diagnosed: threading.Event = field(default_factory=threading.Event)
    diagnostic_lock: threading.Lock = field(default_factory=threading.Lock)
    deferred_stderr: tuple = ()
    reader: threading.Thread | None = None
    reaped: bool = False
    reap_failed: bool = False
    failure: str | None = None
    diagnostic_fault: str | None = None
    unreported_death: bool = False
    exit_code: int | None = None
    preserve_drain: bool = False
    exit_snapshot: tuple = ()


@dataclass(eq=False)
class StartIntent:
    number: int
    freq_hz: int | None
    done: threading.Event = field(default_factory=threading.Event)
    spawning: bool = False
    cancelled: bool = False
    run: Run | None = None
    spawn: object = None
    args: tuple = ()


class SupervisedReceiver:
    """Control operations publish intent under _life and wait outside it."""

    STOP_WAIT_S = 3.0
    EOF_WAIT_S = STREAM_EOF_S
    RETIRE_BEFORE_FENCE = False

    def _init_lifecycle(self):
        self._life = threading.RLock()
        self._run = None
        self._intent = None
        self._control = 0
        self._released = False
        self._closing = False
        self._reports = set()
        self._reports_lock = threading.Lock()

    @property
    def _proc(self):
        run = self._run
        return run.proc if run and not run.ready.is_set() else None

    @property
    def _thread(self):
        return self._run.reader if self._run else None

    @_thread.setter
    def _thread(self, reader):
        self._run.reader = reader

    @property
    def running(self):
        run = self._run
        return bool(run and not run.cancel.is_set() and not run.physical.is_set()
                    and run.proc.poll() is None)

    def live_command(self):
        return childproc.running_command(self._proc) if self.running else None

    @property
    def released(self):
        return self._released

    @property
    def error(self):
        run = self._run
        if run and not run.reaped and run.failure:
            return run.failure
        return self._error

    @error.setter
    def error(self, message):
        self._error = message

    def resume(self):
        with self._life:
            self._released = False

    def _clear(self):
        """A finite capture keeps its saved output across controls."""

    def release(self):
        with self._life:
            self._released = True
            run, intent = self._cancel()
            with self._runs:
                self._clear()
        self._wait_physical(run, intent)

    def retune(self, freq_hz):
        with self._life:
            freq_hz = int(freq_hz)
            if freq_hz == self.freq_hz:
                return
            run, intent = self._cancel()
            with self._runs:
                self.freq_hz = freq_hz
                self._clear()
        self._wait_physical(run, intent)

    def stop(self):
        with self._life:
            run, intent = self._cancel()
        run = self._wait_physical(run, intent)
        if run and run.reaped and run.watch.finite:
            if not run.complete.wait(DIRECT_CAPTURE_WAIT_S):
                raise self._unavailable("receiver is finishing its capture")

    def shutdown(self):
        with self._life:
            self._closing = True
            run, intent = self._cancel()
        self._wait_physical(run, intent)
        with self._reports_lock:
            reports = tuple(self._reports)
        # Older runs can still be reporting. Give the whole set one budget.
        deadline = time.monotonic() + SHUTDOWN_REPORT_WAIT_S
        reported = all(ticket.complete.wait(max(0, deadline - time.monotonic()))
                       for ticket in reports)
        if reason := self.release_failure(pending=True):
            raise self._unavailable(reason)
        if not reported:
            raise self._unavailable(f"{self.watch.tool} shutdown reports still pending")

    def _cancel_intent(self):
        self._control += 1
        intent = self._intent
        if intent:
            intent.cancelled = True
        if intent and not intent.spawning:
            self.discard_start(intent)
            intent = None
        return intent

    def _cancel(self):
        intent = self._cancel_intent()
        run = self._run
        if run:
            if run.watch.finite and not run.cancel.is_set() and not run.physical.is_set():
                # A clean final exit visible before teardown remains eligible for publication.
                try:
                    run.exit_code = run.proc.poll()
                except (OSError, ValueError) as exc:
                    run.failure = f"{run.watch.tool} exit could not be observed: {exc}"
                if run.exit_code is not None and not run.exit_snapshot:
                    final = childproc._stages(run.proc)[-1]
                    run.exit_snapshot = ((final, run.exit_code),)
                    self._record_death(run, run.exit_snapshot, run.watch.clock())
            run.preserve_drain = run.watch.finite and run.reader is not None and run.exit_code == 0
            run.cancel.set()
            if not run.preserve_drain:
                self._fence(run)
            run.wake.set()
        return run, intent

    def _fence(self, run):
        self._runs.end(run.watch, expected=run.number)

    def _wait_physical(self, run, intent):
        deadline = time.monotonic() + PHYSICAL_WAIT_S
        if intent and intent.done.wait(max(0, deadline - time.monotonic())):
            run = intent.run or run
        if run:
            run.physical.wait(max(0, deadline - time.monotonic()))
        return run

    def release_failure(self, *, pending=False):
        run = self._run
        if run and not run.reaped:
            if run.failure:
                return run.failure
            if pending and run.cancel.is_set():
                return f"{run.watch.tool} is still releasing the radio"
        intent = self._intent
        if pending and intent and intent.spawning and intent.cancelled:
            return f"{self.watch.tool} is still opening the radio"

    def _admit(self):
        hook = self.on_reception
        sink = getattr(self, "reception_sink", None)
        if hook is not None and (sink is None or sink.admit()):
            return hook, sink

    def _emit(self, admitted, item, **provenance):
        if admitted is not None:
            hook, sink = admitted
            try:
                childproc.emit_reception(hook, item, **provenance)
            finally:
                if sink is not None:
                    sink.finished()

    def prepare_start(self, *, for_claim=False, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + SAME_TARGET_WAIT_S
        control = self._control
        while True:
            with self._life:
                if self._closing or control != self._control:
                    raise self._unavailable("receiver start was cancelled")
                if self._released and not for_claim:
                    raise self._unavailable(self._released_reason)
                run, other = self._run, self._intent
                if not other and (not run or run.ready.is_set()):
                    intent = StartIntent(self._runs.current + 1, getattr(self, "freq_hz", None))
                    self._intent = intent
                    break
            if not other and not run.cancel.is_set() and run.proc.poll() is None:
                with self._life:
                    if control == self._control and self._run is run and not run.physical.is_set():
                        return None
            if run and run.failure:
                raise self._unavailable(run.failure)
            ticket = other.done if other else run.ready
            if run:
                run.wake.set()
            if not ticket.wait(max(0, deadline - time.monotonic())):
                reason = self.watch.reason or "receiver is finishing its previous run"
                raise self._unavailable(reason)
        try:
            if run and run.unreported_death:
                run.diagnosed.wait(max(0, min(SUPERVISOR_POLL_S, deadline - time.monotonic())))
            intent.spawn, intent.args = self._prepare_launch(intent.freq_hz, intent.number)
            with self._life:
                if self._intent is not intent or control != self._control:
                    raise self._unavailable("receiver start was cancelled")
                if run and run.unreported_death:
                    run.unreported_death = False
                    reason, self.error = run.watch.reason, None
                    raise self._unavailable(reason)
                if self.error:
                    reason, self.error = self.error, None
                    raise self._unavailable(reason)
                if blocked := self.watch.blocked():
                    raise self._unavailable(blocked)
            return intent
        except BaseException:
            self.discard_start(intent)
            raise

    def discard_start(self, intent):
        if intent is None:
            return
        with self._life:
            if self._intent is intent:
                self._intent = None
            intent.done.set()

    def validate_start(self, intent):
        """Reject a cancelled preflight before the caller stands another receiver down."""
        with self._life:
            if self._closing or (intent is not None and self._intent is not intent):
                raise self._unavailable("receiver start was cancelled")
        if intent is None:
            if not self.running:
                raise self._unavailable("receiver is finishing its previous run")

    def launch_start(self, intent, *, claimed=False):
        if intent is None:
            self.validate_start(intent)
            return
        try:
            with self._life:
                if self._closing or self._intent is not intent or (self._released and not claimed):
                    raise self._unavailable("receiver start was cancelled")
                if claimed:
                    self._released = False
                control = self._control
                intent.spawning = True
                self._runs.end(self.watch)
            launch_failure = None
            try:
                proc = intent.spawn()
            except PartialLaunch as exc:
                launch_failure = exc
                proc = exc.proc
            with self._life:
                self._run = run = intent.run = Run(proc, intent.number, self.watch, intent.args)
                self.watch.spawned(proc)
                with self._reports_lock:
                    self._reports.add(run)
                self.error = None
                cancelled = control != self._control or launch_failure is not None
                if cancelled:
                    run.cancel.set()
                    self._fence(run)
                if self._read is not None and launch_failure is None:
                    run.reader = daemon(self._read_run, run,
                                        name=f"sigint101-{self.watch.tool}-reader")
                daemon(self._supervise, run, name=f"sigint101-{self.watch.tool}-supervisor")
            if cancelled:
                self._wait_physical(run, None)
                if launch_failure is not None:
                    reason = f"cannot finish starting {run.watch.tool}: {launch_failure}"
                    raise self._unavailable(run.failure or reason) from launch_failure
                raise self._unavailable("receiver start was cancelled")
        finally:
            self.discard_start(intent)

    def start(self):
        self.launch_start(self.prepare_start())

    def _read_run(self, run):
        try:
            self._read(run.proc, run.number, *run.args)
        finally:
            run.wake.set()
            if not hasattr(run.proc, "close_transport"):
                with self._runs:
                    current = self._runs.is_current(run.number)
                if current:
                    run.wake.set()
                close = getattr(getattr(run.proc, "stdout", None), "close", None)
                if close is not None:
                    try:
                        close()
                    except (OSError, ValueError):
                        pass

    def _statuses(self, run):
        return [(stage, stage.poll()) for stage in childproc._stages(run.proc)]

    def _observe_exit(self, run):
        deadline = None
        first_exit = None
        observed = []
        while True:
            run.wake.clear()
            statuses = self._statuses(run)
            ended = [(stage, code) for stage, code in statuses if code is not None]
            with self._life:
                if run.cancel.is_set():
                    return run.exit_snapshot or observed, first_exit
                if ended and deadline is None:
                    first_exit = run.watch.clock()
                    deadline = time.monotonic() + self.EOF_WAIT_S
                    if not run.watch.finite:
                        observed = ended
                        self._record_death(run, ended, first_exit)
                if ended and (statuses[-1][1] is not None or time.monotonic() >= deadline):
                    run.exit_code = statuses[-1][1]
                    run.exit_snapshot = tuple(ended)
                    if not run.unreported_death:
                        self._record_death(run, ended, first_exit)
                    return ended, first_exit
            run.wake.wait(SUPERVISOR_POLL_S)

    def _record_death(self, run, ended, observed_at):
        reason = childproc.describe_exits(ended, finite=run.watch.finite,
                                         tail=(), tool=run.watch.tool)
        with self._runs:
            if self._runs.is_current(run.number):
                run.unreported_death = bool(run.watch.observed(run.proc, reason, observed_at))
                if run.unreported_death and self.error is None:
                    self.error = reason
        return reason

    def _diagnostic_tail(self, run):
        return None

    def _needs_diagnostics(self, run):
        return run.unreported_death

    def _describe_diagnostics(self, run, ended):
        return childproc.describe_exits(ended, finite=run.watch.finite,
                                        tail=self._diagnostic_tail(run), tool=run.watch.tool)

    def _diagnose(self, run, ended, status):
        try:
            try:
                detail = self._describe_diagnostics(run, ended)
            except Exception as exc:
                run.diagnostic_fault = str(exc)
                detail = f"{status} (exit diagnostic unavailable: {exc})" if status else None
            if detail:
                run.watch.enrich(run.proc, detail)
                with self._runs:
                    if self._runs.is_current(run.number) and self.error == status:
                        self.error = detail
        finally:
            with run.diagnostic_lock:
                run.diagnosed.set()
                streams, run.deferred_stderr = run.deferred_stderr, ()
            self._close_stderr(streams)

    def _stop_child(self, proc):
        stop = getattr(proc, "stop_child", None)
        if stop is None:
            childproc.stop_process(proc)
        else:
            stop()

    def _stop_run(self, run):
        self._stop_child(run.proc)

    def _notify_physical(self, run):
        run.physical.set()

    def _unreaped_reason(self, run):
        return f"{run.watch.tool} survived kill; the dongle may still be held"

    def _retire_reaped(self, run, observed_at):
        """Finish run-specific retirement before making this run ready for replacement."""

    def _close_transport(self, proc):
        close = getattr(proc, "close_transport", None)
        if close is not None:
            close()

    def _drain_reader(self, run):
        def cancelled():
            return run.cancel.is_set() and not run.preserve_drain

        if run.reader is None or cancelled():
            return

        def join():
            try:
                run.reader.join(timeout=self.STOP_WAIT_S)
            finally:
                done.set()
                run.wake.set()

        done = threading.Event()
        daemon(join, name=f"sigint101-{run.watch.tool}-drain")
        while not cancelled() and not done.is_set():
            run.wake.clear()
            if not cancelled() and not done.is_set():
                run.wake.wait()

    def _supervise(self, run):
        ended, observed_at = [], None
        try:
            ended, observed_at = self._observe_exit(run)
            if self._needs_diagnostics(run):
                daemon(self._diagnose, run, ended, run.watch.reason,
                       name=f"sigint101-{run.watch.tool}-diagnostics")
            else:
                run.diagnosed.set()
        except Exception as exc:
            run.failure = f"{run.watch.tool} exit could not be observed: {exc}"
            run.diagnosed.set()
        if run.cancel.is_set():
            try:
                self._close_transport(run.proc)
            except Exception as exc:
                # The process must still be stopped. The final close verifies the transport.
                run.failure = f"{run.watch.tool} transport could not close: {exc}"
        try:
            self._stop_run(run)
            reaped = all(code is not None for _, code in self._statuses(run))
            if reaped and not hasattr(run.proc, "close_transport"):
                run.reaped = True
                self._notify_physical(run)
            if reaped and (ended or run.preserve_drain):
                self._drain_reader(run)
        except Exception as exc:
            reaped = False
            run.failure = f"{run.watch.tool} cleanup failed: {exc}"
        finally:
            if not self.RETIRE_BEFORE_FENCE or not reaped:
                self._fence(run)
            try:
                self._close_transport(run.proc)
            except Exception as exc:
                reaped = False
                run.failure = f"{run.watch.tool} transport could not close: {exc}"
            run.reaped = reaped
            run.reap_failed = not reaped
            if not reaped and not run.failure:
                run.failure = self._unreaped_reason(run)
            self._notify_physical(run)
        try:
            if not run.reaped:
                self._retry(run)
            if run.reaped:
                try:
                    self._retire_reaped(run, observed_at)
                finally:
                    if self.RETIRE_BEFORE_FENCE:
                        self._fence(run)
                self._report(run)
        finally:
            run.ready.set()
            try:
                if run.reaped:
                    self._close_diagnostics(run)
            finally:
                run.complete.set()
                with self._reports_lock:
                    self._reports.discard(run)

    def _close_diagnostics(self, run):
        # DMR exposes its stderr as the reader's stdout. That reader still owns its stream.
        reader_stream = getattr(run.proc, "stdout", None)
        streams = []
        for stage in childproc._stages(run.proc):
            stderr = getattr(stage, "stderr", None)
            if stderr is not None and stderr is not reader_stream:
                streams.append(stderr)
        if not streams:
            return
        if not run.diagnosed.wait(DIAGNOSTIC_WAIT_S):
            # The diagnostic worker still owns stderr. Give it the close when it finishes.
            with run.diagnostic_lock:
                if not run.diagnosed.is_set():
                    run.deferred_stderr = tuple(streams)
                    return
        self._close_stderr(streams)

    @staticmethod
    def _close_stderr(streams):
        for stream in streams:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _retry_reap(self, run, timeout):
        deadline = time.monotonic() + timeout
        try:
            for stage, code in self._statuses(run):
                if code is None:
                    stage.kill()
            for stage, code in self._statuses(run):
                if code is None:
                    stage.wait(timeout=max(0, deadline - time.monotonic()))
            if all(code is not None for _, code in self._statuses(run)):
                self._close_transport(run.proc)
                run.reaped = True
                self._notify_physical(run)
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        except Exception as exc:
            run.failure = f"{run.watch.tool} cleanup retry failed: {exc}"
        return False

    def _retry(self, run):
        while not self._closing:
            run.wake.clear()
            if self._closing:
                return
            if not run.wake.wait(REAP_RETRY_MIN_S) and self._retry_reap(run, REAP_RETRY_WAIT_S):
                return

    def _report(self, run):
        """Streaming receivers already report each item from their reader."""
        run.ready.set()

    def drain(self, timeout=5.0):
        run = self._run
        if run is not None:
            deadline = time.monotonic() + timeout
            if run.reader is not None:
                run.reader.join(timeout)
            if run.proc.poll() is not None:
                run.wake.set()
                run.ready.wait(max(0, deadline - time.monotonic()))
