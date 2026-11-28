# ABOUTME: Runs one live Meteor-M LRPT pass with SatDump under the shared receiver lifecycle.
# ABOUTME: Separates radio release from finishing, stderr drain, and result classification.

from __future__ import annotations

import fcntl
import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from . import childproc, receiver_lifecycle
from .hardware import (
    RTLSDR_SERIAL_ENV,
    DeviceSelectionError,
    describe_devices,
    list_rtlsdr_devices,
    probe_rtlsdr,
    rtlsdr_radio_for_run,
)
from .receiver_lifecycle import PartialLaunch, SupervisedReceiver

SATDUMP = "satdump"
MODE_ENV = "SIGINT101_METEOR_MODE"
HZ_ENV = "SIGINT101_METEOR_HZ"
DEFAULT_MODE = "72k"
DEFAULT_HZ = 137_900_000
PIPELINES = {"72k": "meteor_m2-x_lrpt", "80k": "meteor_m2-x_lrpt_80k"}
CHANNELS = ("137900000", "137100000")
CAPTURE_S = 900
FINISH_CAP_S = 600  # Measure the longest product processing time on the kiosk.
RELEASE_GRACE_S = 3.0
KILL_GRACE_S = 3.0
FINISH_NICE = 10

_CLOSE_MARKER = "Stop processing"
_LOG_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}(?: - \d{2}/\d{2}/\d{4})?\] \([TDIWEC]\) ")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_STATUS = re.compile(
    r"Progress [^,]+%,\s*Viterbi\s*:\s*(SYNCED|NOSYNC)\s+BER\s*:\s*[^,]+,"
    r"\s*Deframer\s*:\s*(NOSYNC|SYNCING|SYNCED)"
)
_PRODUCT = re.compile(r"MSU[-_]MR[-_]([A-Za-z0-9][A-Za-z0-9 _-]{0,95})\.png")
_DIR_FLAGS = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_PNG = b"\x89PNG\r\n\x1a\n"
_MAX_LINE = 1024
_MAX_ENTRIES = 4096
_MAX_DEPTH = 8


class MeteorUnavailable(RuntimeError):
    """SatDump cannot start or relinquish its RTL-SDR."""


def _settings():
    mode = os.environ.get(MODE_ENV, DEFAULT_MODE)
    hz = os.environ.get(HZ_ENV, str(DEFAULT_HZ))
    if mode not in PIPELINES:
        raise MeteorUnavailable(f"{MODE_ENV} must be 72k or 80k.")
    if hz not in CHANNELS:
        raise MeteorUnavailable(f"{HZ_ENV} must be 137900000 or 137100000.")
    return mode, int(hz)


@contextmanager
def _directory(path, parent=None, *, create=False, private=True):
    if create:
        try:
            os.mkdir(path, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(path, _DIR_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if private and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise PermissionError("Meteor staging must be owned by the console and private")
        yield fd
    finally:
        os.close(fd)


@dataclass
class _Stage:
    output: Path
    parent: int
    fd: int
    lock_fd: int

    @classmethod
    def create(cls, path: Path, work: int, name: str):
        # The visible owner name only appears after the lock is held. SatDump inherits the lock.
        pending = f".pending-{name}"
        os.mkdir(pending, 0o700, dir_fd=work)
        fd = lock = parent = None
        named = False
        try:
            fd = os.open(pending, _DIR_FLAGS, dir_fd=work)
            lock = os.open(".owner.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                           0o600, dir_fd=fd)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            parent = os.dup(work)
            os.rename(pending, name, src_dir_fd=work, dst_dir_fd=work)
            named = True
            stage = cls(path / name, parent, fd, lock)
            stage.verify()
            return stage
        except BaseException:
            if lock is not None:
                os.close(lock)
                os.unlink(".owner.lock", dir_fd=fd)
            if fd is not None:
                os.close(fd)
            if parent is not None:
                os.close(parent)
            os.rmdir(name if named else pending, dir_fd=work)
            raise

    def verify(self):
        if not os.path.samestat(self.output.stat(follow_symlinks=False), os.fstat(self.fd)):
            raise OSError("Meteor staging directory changed")

    def close(self):
        # This is the exact retired run, with its ownership lock still held. Pruning other
        # owners and retaining published output belong to gallery publication.
        try:
            info = os.fstat(self.fd)
            _clear_run(self.fd, info.st_dev, 0, [_MAX_ENTRIES])
            if os.path.samestat(os.stat(self.output.name, dir_fd=self.parent,
                                        follow_symlinks=False), info):
                os.unlink(".owner.lock", dir_fd=self.fd)
                os.rmdir(self.output.name, dir_fd=self.parent)
        finally:
            for fd in (self.lock_fd, self.fd, self.parent):
                os.close(fd)


def _clear_run(fd, device, depth, budget):
    """Discard an owned pass by directory descriptor, leaving unknown data in place."""
    if depth >= _MAX_DEPTH:
        raise OSError("Meteor work directory exceeded its cleanup depth")
    with os.scandir(fd) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                raise OSError("Meteor work directory exceeded its cleanup size")
            if depth == 0 and entry.name == ".owner.lock":
                continue
            info = entry.stat(follow_symlinks=False)
            if info.st_dev != device or info.st_uid != os.geteuid():
                raise PermissionError("Meteor left a foreign work entry in place")
            if stat.S_ISDIR(info.st_mode):
                with _directory(entry.name, fd, private=False) as nested:
                    if not os.path.samestat(os.fstat(nested), info):
                        raise OSError("Meteor work entry changed during cleanup")
                    _clear_run(nested, device, depth + 1, budget)
                if not os.path.samestat(os.stat(entry.name, dir_fd=fd, follow_symlinks=False),
                                        info):
                    raise OSError("Meteor work entry changed during cleanup")
                os.rmdir(entry.name, dir_fd=fd)
            else:
                os.unlink(entry.name, dir_fd=fd)


def _pictures(fd, device, prefix=(), budget=None):
    """Inspect only bounded, same-owner, same-device contents beneath the owned pass."""
    if budget is None:
        budget = [_MAX_ENTRIES]
    if len(prefix) >= _MAX_DEPTH or budget[0] <= 0:
        return
    with os.scandir(fd) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                break
            try:
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device or info.st_uid != os.geteuid():
                    continue
                parts = (*prefix, entry.name)
                if stat.S_ISDIR(info.st_mode):
                    with _directory(entry.name, fd, private=False) as nested:
                        if os.path.samestat(os.fstat(nested), info):
                            yield from _pictures(nested, device, parts, budget)
                elif stat.S_ISREG(info.st_mode) and (product := _PRODUCT.fullmatch(entry.name)):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                    with os.fdopen(os.open(entry.name, flags, dir_fd=fd), "rb") as image:
                        current = os.fstat(image.fileno())
                        if (os.path.samestat(current, info) and current.st_size > len(_PNG)
                                and image.read(len(_PNG)) == _PNG):
                            yield {"path": "/".join(parts), "product": product[1]}
            except OSError:
                continue


@dataclass
class _Capture:
    id: str
    mode: str
    hz: int
    radio: dict
    argv: tuple = ()
    stage: _Stage | None = None
    started_at: float | None = None
    launched: float = 0
    deadline: float = 0
    control: threading.Lock = field(default_factory=threading.Lock)
    eof: threading.Event = field(default_factory=threading.Event)
    handoff_done: threading.Event = field(default_factory=threading.Event)
    retry_now: threading.Event = field(default_factory=threading.Event)
    handoff_deadline: float | None = None
    kill_deadline: float | None = None
    term_sent: bool = False
    marker_at: float | None = None
    ended_at: float | None = None
    ended_by: str | None = None
    interrupted: str | None = None
    reports: tuple[str, str] | None = None
    lock_ever: bool = False
    tail: deque = field(default_factory=lambda: deque(maxlen=3))
    result: tuple | None = None
    pictures: tuple = ()

    @property
    def output(self):
        return self.stage.output

    @property
    def lock(self):
        if self.reports == ("SYNCED", "SYNCED"):
            return "locked"
        return "searching" if self.reports else "unknown"


class _SatDumpProcess:
    """Keep stderr with the sole reader. Forward group operations to the real child."""

    def __init__(self, proc):
        if not callable(getattr(proc.stderr, "readline", None)):
            raise ValueError("SatDump has no readable stderr pipe")
        self.proc = proc
        self.stdout = proc.stderr

    def __getattr__(self, name):
        return getattr(self.proc, name)


class MeteorDecoder(SupervisedReceiver):
    RETIRE_BEFORE_FENCE = True
    _unavailable = MeteorUnavailable
    _released_reason = "the radio was released to another program; use Listen now to take it back"

    def __init__(self, *, data_dir, radio_present=None, list_devices=list_rtlsdr_devices,
                 spawn=childproc.spawn, which=shutil.which, clock=time.time,
                 monotonic=time.monotonic, sync_assets=False):
        self.data_dir = Path(data_dir).resolve()
        self.freq_hz = DEFAULT_HZ  # The next launch reads the site settings.
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._clock = clock
        self._monotonic = monotonic
        self._sync_assets = bool(sync_assets)
        self._runs = childproc.RunGuard()
        self.watch = childproc.ChildWatch(SATDUMP, finite=True, clock=monotonic)
        self._init_lifecycle()
        self.error = None
        self.on_reception = None

    @contextmanager
    def _work(self):
        with (_directory(self.data_dir, private=False) as data,
              _directory("meteor", data, create=True) as root,
              _directory("work", root, create=True) as work):
            yield work

    def _prepare_launch(self, _freq_hz, _number):
        mode, hz = _settings()
        if not self._which(SATDUMP):
            raise MeteorUnavailable("satdump is not installed, so SIGINT 101 cannot decode Meteor")
        if not self._radio_present():
            raise MeteorUnavailable("no RTL-SDR on the USB bus — plug in the RTL-SDR")
        try:
            devices = tuple(self._list_devices())
            pin = os.environ.get(RTLSDR_SERIAL_ENV)
            if pin and sum(device.serial == pin for device in devices) > 1:
                raise DeviceSelectionError(
                    f"serial {pin} matches more than one RTL-SDR. "
                    f"Found: {describe_devices(devices)}")
            radio = rtlsdr_radio_for_run(0, pin, lambda: devices)
            with self._work() as work:
                if not os.access(".", os.W_OK | os.X_OK, dir_fd=work):
                    raise PermissionError("Meteor work directory is not writable")
        except (OSError, ValueError, DeviceSelectionError) as exc:
            raise MeteorUnavailable(f"cannot prepare Meteor: {exc}" if isinstance(exc, OSError)
                                    else str(exc)) from exc
        capture = _Capture(uuid4().hex, mode, hz, radio)
        root = self.data_dir / "meteor"
        output = root / "work" / capture.id
        selector = ("--source_id", radio["serial"]) if radio["serial"] else ()
        capture.argv = (SATDUMP, "live", PIPELINES[mode], str(output), "--source", "rtlsdr",
                        *selector, "--samplerate", "1024000", "--frequency", str(hz),
                        "--gain", "49.6", "--timeout", str(CAPTURE_S),
                        "--tle_override", str(root / "tle.txt"), "--finish_processing")

        def launch():
            try:
                with self._work() as work:
                    capture.stage = _Stage.create(output.parent, work, capture.id)
                capture.launched = self._monotonic()
                capture.started_at = self._clock()
                capture.deadline = capture.launched + CAPTURE_S + FINISH_CAP_S
                proc = self._spawn(capture.argv, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   pass_fds=(capture.stage.lock_fd,))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self._discard_stage(capture)
                raise MeteorUnavailable(f"cannot run SatDump: {exc}") from exc
            try:
                child = _SatDumpProcess(proc)
            except (AttributeError, ValueError) as exc:
                raise PartialLaunch(proc, exc) from exc
            self.freq_hz = hz
            return child

        return launch, (capture,)

    def launch_start(self, intent, *, claimed=False):
        super().launch_start(intent, claimed=claimed)
        return intent is not None

    def _read(self, *_):
        """The supervisor calls the run-aware reader below."""

    def _read_run(self, run):
        capture, = run.args
        try:
            stream = run.proc.stdout
            while line := stream.readline(_MAX_LINE + 1):
                if len(line) > _MAX_LINE:
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(_MAX_LINE + 1)
                    if not line:
                        break
                    continue
                self._line(run, capture, line.decode("utf-8", errors="replace"))
            capture.eof.set()
        except (OSError, ValueError) as exc:
            with capture.control:
                capture.tail.append(f"stderr read failed: {exc}"[:160])
        finally:
            run.wake.set()
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _line(self, run, capture, line):
        message = _LOG_PREFIX.sub("", _ANSI.sub("", line).strip(), count=1)
        with capture.control:
            if capture.result is not None:
                return
            if message:
                capture.tail.append(message[:160])
            if message == _CLOSE_MARKER and capture.marker_at is None:
                if capture.interrupted is None and not run.cancel.is_set() and not self._closing:
                    capture.marker_at = self._monotonic()
                    capture.deadline = min(capture.deadline, capture.marker_at + FINISH_CAP_S)
                    run.physical.set()
                    capture.handoff_done.set()
                    run.wake.set()
            elif match := _STATUS.fullmatch(message):
                capture.reports = match.groups()
                capture.lock_ever |= capture.lock == "locked"

    def release(self):
        deadline = self._monotonic() + RELEASE_GRACE_S + KILL_GRACE_S
        with self._life:
            self._released = True
            intent = self._cancel_intent()
            run = self._run
            if run and not run.ready.is_set():
                self._fence(run)
                capture, = run.args
                with capture.control:
                    if not run.physical.is_set() and capture.handoff_deadline is None:
                        capture.handoff_deadline = deadline - KILL_GRACE_S
                    if capture.handoff_deadline is not None:
                        deadline = min(deadline, capture.handoff_deadline + KILL_GRACE_S)
                if run.reap_failed:
                    capture.retry_now.set()
                run.wake.set()
        if intent:
            intent.done.wait(max(0, deadline - self._monotonic()))
            run = intent.run or run
        if run and not run.ready.is_set():
            run.args[0].handoff_done.wait(max(0, deadline - self._monotonic()))

    def release_failure(self, *, pending=False):
        intent, run = self._intent, self._run
        if pending and intent and intent.spawning and intent.cancelled and intent.run is None:
            return "SatDump is still opening the radio"
        if run and not run.physical.is_set():
            if run.failure:
                return run.failure
            if pending:
                return "SatDump is still releasing the radio"

    def _cancel(self):
        intent = self._cancel_intent()
        run = self._run
        if run and not run.ready.is_set():
            self._fence(run)
            capture, = run.args
            with capture.control:
                if self._closing:
                    self._interrupt(run, capture, "shutdown", "Console shut down.")
                else:
                    self._interrupt(run, capture, "stopped", "SatDump was stopped.")
            if run.reap_failed:
                capture.retry_now.set()
            run.wake.set()
        return run, intent

    def _interrupt(self, run, capture, cause, reason):
        if capture.interrupted is None:
            capture.ended_by = cause
            capture.interrupted = reason
        run.cancel.set()

    def _observe_exit(self, run):
        capture, = run.args
        priority_set = False
        while True:
            run.wake.clear()
            code = run.proc.poll()
            if code is not None:
                return [(run.proc, code)], self._monotonic()
            with capture.control:
                now = self._monotonic()
                if self._closing:
                    self._interrupt(run, capture, "shutdown", "Console shut down.")
                elif run.cancel.is_set() and capture.interrupted is None:
                    self._interrupt(run, capture, "handoff", "SatDump launch was cancelled.")
                elif now >= capture.deadline:
                    self._interrupt(run, capture, "watchdog",
                                    "SatDump exceeded its finishing deadline.")
                if run.cancel.is_set():
                    return [], None
                if run.physical.is_set():
                    lower_priority = not priority_set
                    deadline = capture.deadline
                else:
                    lower_priority = False
                    release = capture.handoff_deadline
                    if release is not None:
                        if now >= release:
                            self._interrupt(run, capture, "handoff",
                                            "SatDump did not release the radio in time.")
                            capture.kill_deadline = release + KILL_GRACE_S
                            return [], None
                        if not capture.term_sent:
                            capture.term_sent = True
                            run.proc.terminate()
                    deadline = capture.deadline if release is None else min(
                        capture.deadline, release)
            if lower_priority:
                priority_set = True
                try:
                    # Linux applies this priority to SatDump's leader thread.
                    run.proc.lower_priority(FINISH_NICE)
                except (OSError, ValueError) as exc:
                    run.diagnostic_fault = f"SatDump priority could not be lowered: {exc}"[:160]
            run.wake.wait(min(receiver_lifecycle.SUPERVISOR_POLL_S,
                              max(0, deadline - self._monotonic())))

    def _statuses(self, run):
        try:
            return super()._statuses(run)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            run.failure = f"{self._unreaped_reason(run)}: {str(exc)[:160]}"
            return [(run.proc, None)]

    def _stop_run(self, run):
        capture, = run.args
        deadline = capture.kill_deadline
        if deadline is None:
            deadline = self._monotonic() + KILL_GRACE_S
        self._kill_and_reap(run, deadline)

    def _wait_for_kill(self, run, deadline):
        try:
            run.proc.wait(timeout=max(0, deadline - self._monotonic()))
        except subprocess.TimeoutExpired:
            run.failure = self._unreaped_reason(run, timed_out=True)
            return False
        if run.proc.poll() is not None:
            return True
        run.failure = self._unreaped_reason(run)
        return False

    def _kill_and_reap(self, run, deadline):
        proc = run.proc
        try:
            if proc.poll() is not None:
                return True
            capture, = run.args
            with capture.control:
                if capture.interrupted is None:
                    self._interrupt(run, capture, "handoff", "SatDump cleanup was interrupted.")
            proc.kill()
            try:
                return self._wait_for_kill(run, deadline)
            except (OSError, ValueError, subprocess.SubprocessError):
                # The first wait failed. Recheck ownership before signalling the exact child.
                if proc.poll() is not None:
                    return True
                proc.kill()
                return self._wait_for_kill(run, deadline)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            run.failure = f"{self._unreaped_reason(run)}: {str(exc)[:160]}"
            return False

    def _unreaped_reason(self, run, *, timed_out=False):
        result = "survived kill" if timed_out else "could not be confirmed stopped"
        if run.physical.is_set():
            return f"SatDump finisher {result}"
        return f"SatDump {result}; the dongle may still be held"

    def _notify_physical(self, run):
        capture, = run.args
        if run.reaped:
            run.physical.set()
        if run.reaped or run.reap_failed:
            capture.handoff_done.set()

    def _retry_reap(self, run, timeout):
        if not self._kill_and_reap(run, self._monotonic() + timeout):
            return False
        run.reaped = True
        self._notify_physical(run)
        return True

    def _retry(self, run):
        # Only controls expedite a reap. Reader wakes and status polls leave the cadence alone.
        pending = run.args[0].retry_now
        while not self._closing:
            pending.wait(receiver_lifecycle.REAP_RETRY_MIN_S)
            pending.clear()
            if not self._closing and self._retry_reap(run, receiver_lifecycle.REAP_RETRY_WAIT_S):
                return
        if not run.reaped:
            self._retry_reap(run, KILL_GRACE_S)

    def _drain_reader(self, _run):
        """Wait for explicit EOF during retirement, including cancelled and retried exits."""

    def _retire_reaped(self, run, observed_at):
        capture, = run.args
        capture.ended_at = self._clock()
        eof = run.reader is not None and capture.eof.wait(receiver_lifecycle.STREAM_EOF_S)
        code = run.exit_code = run.proc.poll()
        if eof and code == 0 and capture.stage is not None and not run.cancel.is_set():
            try:
                fd = capture.stage.fd
                capture.pictures = tuple(_pictures(fd, os.fstat(fd).st_dev))
            except OSError as exc:
                run.diagnostic_fault = f"could not inspect SatDump work: {exc}"[:160]
        with capture.control:
            phase, reason = self._classify(capture, code, eof)
            capture.result = phase, reason
            if phase != "done":
                capture.pictures = ()
            if capture.ended_by is None:
                capture.ended_by = ("handoff" if capture.term_sent else
                                    "timeout" if self._monotonic() >= capture.launched + CAPTURE_S
                                    else "self_exit")
        if phase == "stopped":
            when = self._monotonic() if observed_at is None else observed_at
            with self._runs:
                if self._runs.is_current(run.number):
                    run.unreported_death = bool(run.watch.observed(
                        run.proc, reason, when))
                    if run.unreported_death:
                        self.error = reason
        self._discard_stage(capture)

    @staticmethod
    def _classify(capture, code, eof):
        if capture.interrupted is not None:
            return "interrupted", capture.interrupted
        if not eof:
            return "interrupted", "SatDump stderr did not reach EOF."
        if code == 0:
            if capture.pictures:
                return "done", None
            if capture.marker_at is None:
                return "no_picture", "The close marker was never seen."
            if capture.lock_ever:
                return "no_picture", "The signal locked but no picture was decoded."
            if capture.lock == "unknown":
                return "no_picture", "SatDump reported no lock status or picture."
            return "no_picture", "No signal lock or picture was decoded."
        detail = ": " + capture.tail[-1] if capture.tail else ""
        if capture.marker_at is not None:
            return "no_picture", f"SatDump could not finish decoding (exit {code}){detail}"
        if capture.term_sent:
            return "interrupted", f"SatDump stopped during the radio handoff (exit {code})."
        return "stopped", f"SatDump stopped before releasing the radio (exit {code}){detail}"

    @staticmethod
    def _discard_stage(capture):
        if capture.stage is not None:
            try:
                capture.stage.close()
            except OSError as exc:
                capture.tail.append(f"could not discard SatDump work: {exc}"[:160])

    def shutdown(self):
        # Reuse the report budget, and also check a surviving finisher whose radio is free.
        super().shutdown()
        run = self._run
        if run and not run.reaped:
            raise MeteorUnavailable(run.failure or self._unreaped_reason(run))

    def live_command(self):
        run = self._run
        return shlex.join(run.args[0].argv) if run and not run.ready.is_set() else None

    def snapshot(self):
        run = self._run
        body = {"available": True, "passes": [], "command": None}
        if run is None:
            try:
                mode, hz = _settings()
            except MeteorUnavailable:
                mode, hz = None, None
            return {**body, "state": "idle", "reason": "Ready to listen", "mode": mode,
                    "freq_hz": hz, "lock": "unknown", "lock_ever": False, "radio": None,
                    "started_at": None, "ended_at": None, "ended_by": None, "elapsed_s": None}
        capture, = run.args
        with capture.control:
            lock = capture.lock
            if capture.result is not None:
                state, reason = capture.result
            elif capture.marker_at is not None:
                state, reason = "decoding", None
            elif self.watch.settling():
                state, reason = "starting", None
            elif lock == "locked":
                state, reason = "receiving", None
            else:
                state = "searching"
                reason = f"Listening on {capture.hz / 1e6:.3f} MHz · searching"
            now = self._monotonic()
            elapsed = ((now - capture.marker_at) if state == "decoding" else
                       (now - capture.launched)) if capture.result is None else None
            body.update(state=state, reason=reason, lock=lock, lock_ever=capture.lock_ever,
                        mode=capture.mode, freq_hz=capture.hz, radio=capture.radio.copy(),
                        command=shlex.join(capture.argv) if capture.result is None else None,
                        started_at=capture.started_at, ended_at=capture.ended_at,
                        ended_by=capture.ended_by, elapsed_s=elapsed)
        if state == "stopped":
            body["retry_in"] = round(self.watch.retry_in() or 0, 1)
        return body
