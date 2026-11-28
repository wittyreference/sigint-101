# ABOUTME: Records one finite RTL-SDR sample stream as a private SigMF pair.
# ABOUTME: C40 owns radio teardown while this receiver verifies and publishes the finished stream.

import hashlib
import json
import math
import os
import re
import select
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import childproc
from .capture_store import CaptureStorageError, CaptureStore
from .hardware import DeviceSelectionError, parse_rtlsdr_inventory, rtlsdr_radio_for_run
from .operate import command_gain, instrument, validate_decoder_hz
from .receiver_lifecycle import SupervisedReceiver
from .spectrum import TuneError, validate_gain

TOOL = "rtl_sdr"
RATE = 250_000
SECONDS = 9
SAMPLES = RATE * SECONDS
BYTES = 2 * SAMPLES
CAPTURE_TIMEOUT_S = 15.0
DIAG_BYTES = 8192
_FOUND = re.compile(r"Found (\d+) device\(s\):")
_USING = re.compile(r"Using device (\d+):.*")
_USB_FAILURE = re.compile(r"usb.*(?:error|fail)|rtlsdr_read_async.*(?:returned with|error)|"
                          r"short write, samples lost|error during transfer", re.I)
_TUNER_FAILURE = re.compile(r"failed to (?:set|reset|allocate)|pll not locked|"
                            r"buffer.*(?:error|failed)|tuner.*(?:error|failed)", re.I)


class CaptureUnavailable(RuntimeError):
    def __init__(self, message, *, status=503):
        super().__init__(message)
        self.status = status


@dataclass
class _Recording:
    id: str
    tune: dict
    settings: object
    ppm: object
    cancelled: threading.Event = field(default_factory=threading.Event)
    data: bytearray = field(default_factory=bytearray)
    state: str = "starting"
    busy: bool = True
    spawned: bool = False
    commit_admitted: bool = False
    deadline: float | None = None
    timer: threading.Timer | None = None
    launched_at: str | None = None
    arrived_at: str | None = None
    reason: str | None = None
    read_error: str | None = None
    eof: bool = False
    count: int = 0
    argv: tuple = ()
    radio: dict | None = None
    census_size: int = 0


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_tune(tune):
    if (not isinstance(tune, dict) or set(tune) != {"hz", "gain"}
            or type(tune["hz"]) is not int
            or (tune["gain"] is not None and not isinstance(tune["gain"], str))):
        raise CaptureUnavailable("Confirm the Waterfall frequency and gain", status=400)
    try:
        validate_decoder_hz(instrument("ism"), tune["hz"])
        gain = validate_gain(tune["gain"])
    except (TuneError, OverflowError) as exc:
        raise CaptureUnavailable(str(exc), status=400) from exc
    if gain is not None and int(gain * 10) == 0:
        raise CaptureUnavailable("Record with Auto or a different manual gain", status=400)


def _matches_radio(head, radio, census_size):
    lines = head.splitlines()
    counts = [int(match[1]) for line in lines if (match := _FOUND.fullmatch(line))]
    selections = [int(match[1]) for line in lines if (match := _USING.fullmatch(line))]
    devices = parse_rtlsdr_inventory(lines)
    if len(counts) != 1 or selections != [radio["index"]] or len(devices) != counts[0]:
        return False
    if {d.index for d in devices} != set(range(len(devices))) or selections[0] >= len(devices):
        return False
    serial = radio["serial"] or ""
    matches = [d for d in devices if d.serial == serial]
    return (len(matches) == 1 and matches[0].index == selections[0]
            and (bool(serial) or census_size == len(devices) == 1))


def _sigmf(ticket):
    command = shlex.join(ticket.argv)
    provenance = {
        "origin": "learner_capture", "instrument": "ism", "tool": TOOL,
        "freq_hz": ticket.tune["hz"], "gain": command_gain(command),
        "ppm": {"correction": "raw", "measured_ppm": ticket.ppm},
        "radio": ticket.radio, "command": command, "summary": "Local IQ capture",
        "item": {"samples": SAMPLES, "seconds": SECONDS},
        "timestamp_basis": "First stdout arrival on the console UTC clock",
        "launched_at": ticket.launched_at,
    }
    return json.dumps({
        "global": {
            "core:datatype": "cu8", "core:sample_rate": RATE, "core:hw": "RTL-SDR",
            "core:version": "1.0.0", "core:sha512": hashlib.sha512(ticket.data).hexdigest(),
            "core:extensions": [{"name": "sigint101", "version": "1.0.0", "optional": False}],
            "sigint101:instrument": "ism", "sigint101:source": f"Console capture · {command}",
            "sigint101:provenance": provenance,
        },
        "captures": [{"core:sample_start": 0, "core:frequency": ticket.tune["hz"],
                      "core:datetime": ticket.arrived_at}],
        "annotations": [],
    }, ensure_ascii=False, allow_nan=False).encode("utf-8")


class Capture(SupervisedReceiver):
    _unavailable = CaptureUnavailable
    _released_reason = "The radio was released to another program"
    on_reception = None

    def __init__(self, data_dir, *, list_devices, radio_present, serial=None,
                 which=shutil.which, spawn=childproc.spawn):
        self.store = CaptureStore(data_dir)
        self._list_devices, self._radio_present = list_devices, radio_present
        self._serial, self._which, self._spawn = serial, which, spawn
        self.watch = childproc.ChildWatch(TOOL, finite=True)
        self.error = None
        self._runs = childproc.RunGuard()
        self._ticket = None
        self._pending = None  # A start request can be preparing without owning the radio.
        self._last_saved = None
        self._controls = 0
        self.on_event = None
        self._init_lifecycle()

    def _event(self, kind, ticket, **fields):
        if self.on_event is not None:
            self.on_event(kind, {"run_id": ticket.id, **fields})

    def request_start(self, tune, *, settings, ppm, claim):
        _validate_tune(tune)
        if tune != settings():
            raise CaptureUnavailable("Confirm the current Waterfall dial", status=409)
        ticket = _Recording(uuid.uuid4().hex, dict(tune), settings, ppm)
        with self._life:
            if reason := self.release_failure():
                raise CaptureUnavailable(reason)
            previous = self._ticket
            if (self._closing or self._controls or self._pending
                    or (previous is not None and previous.busy)):
                raise CaptureUnavailable("A capture is already in progress", status=409)
            self._pending = ticket
        try:
            if not self._which(TOOL):
                raise CaptureUnavailable("rtl_sdr is not installed")
            if not self._radio_present():
                raise CaptureUnavailable("no RTL-SDR on the USB bus")
            census = tuple(self._list_devices())
            radio = rtlsdr_radio_for_run(0, self._serial, lambda: census)
            if (len({d.index for d in census}) != len(census)
                    or (radio["serial"] and sum(d.serial == radio["serial"] for d in census) != 1)):
                raise CaptureUnavailable("The radio census is ambiguous")
            ticket.census_size, ticket.radio = len(census), radio
            self.store.preflight()
            claim()
        except CaptureStorageError as exc:
            raise CaptureUnavailable(str(exc), status=507 if exc.full else 503) from exc
        except DeviceSelectionError as exc:
            raise CaptureUnavailable(str(exc)) from exc
        except CaptureUnavailable:
            if not (ticket.cancelled.is_set() and ticket.state == "stopped"):
                raise
        finally:
            with self._life:
                if not ticket.spawned:
                    ticket.busy = False
                    if self._ticket is ticket:
                        self._ticket = previous
                    if ticket.timer is not None:
                        ticket.timer.cancel()
                self._pending = None
        return self.snapshot(settings())

    def validate_start(self, intent):
        super().validate_start(intent)
        ticket, = intent.args
        with self._life:
            if not ticket.busy or ticket.cancelled.is_set() or ticket is not self._pending:
                raise CaptureUnavailable("Capture start was cancelled")
        if ticket.tune != ticket.settings():
            raise CaptureUnavailable("Confirm the current Waterfall dial", status=409)
        measured = ticket.ppm()
        ticket.ppm = measured if measured is not None and math.isfinite(measured) else None

    def _prepare_launch(self, _hz, _number):
        with self._life:
            ticket = self._pending
            if ticket is None or not ticket.busy or ticket.cancelled.is_set():
                raise CaptureUnavailable("Capture start was cancelled")
        hz, gain = ticket.tune["hz"], ticket.tune["gain"]
        argv = [TOOL, "-d", str(ticket.radio["index"]), "-f", str(hz), "-s", str(RATE),
                "-n", str(SAMPLES)]
        if gain is not None:
            argv += ["-g", gain]
        ticket.argv = (*argv, "-")

        def launch():
            with self._life:
                if ticket.cancelled.is_set():
                    raise CaptureUnavailable("Capture start was cancelled")
                # Preflight and claim admission succeeded. Only now protect the radio.
                self._ticket = ticket
                ticket.launched_at = _utc()
                ticket.deadline = time.monotonic() + CAPTURE_TIMEOUT_S
                ticket.timer = threading.Timer(CAPTURE_TIMEOUT_S, self._expire, (ticket,))
                ticket.timer.daemon = True
                ticket.timer.start()
            try:
                proc = self._spawn(list(ticket.argv), stdin=subprocess.DEVNULL,
                                   env={"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"})
            except (ValueError, OSError) as exc:
                raise CaptureUnavailable(f"Cannot start {TOOL}: {exc}") from exc
            with self._life:
                ticket.spawned = True
                if not ticket.cancelled.is_set():
                    ticket.state = "recording"
            return proc

        return launch, (ticket,)

    def _cancel_recording(self, ticket, state, reason):
        if ticket and ticket.busy and not ticket.commit_admitted and not ticket.cancelled.is_set():
            ticket.cancelled.set()
            ticket.state, ticket.reason = state, reason

    def _expire(self, ticket):
        with self._life:
            if ticket is not self._ticket or not ticket.busy or ticket.deadline is None:
                return
            if self._run and self._run.args[0] is ticket and self._run.reaped and ticket.eof:
                return
            self._cancel_recording(ticket, "error", "The capture deadline expired")
            self._controls += 1
        try:
            super().stop()
        except CaptureUnavailable:
            pass  # Status and release_failure carry the result for this exact run.
        finally:
            with self._life:
                self._controls -= 1

    def _control_capture(self, control):
        with self._life:
            self._controls += 1
            self._cancel_recording(self._pending or self._ticket, "stopped", "Capture stopped")
        try:
            control()
        finally:
            with self._life:
                self._controls -= 1

    def stop(self):
        with self._life:
            if self._ticket and self._ticket.busy and self._ticket.commit_admitted:
                return  # Commit won after confirmed reap. The report owns publication.
        self._control_capture(super().stop)

    def shutdown(self):
        self._control_capture(super().shutdown)

    def _read(self, proc, number, ticket):
        with self._runs:
            if not self._runs.is_current(number) or ticket.cancelled.is_set():
                return
        self._event("capture_start", ticket, freq_hz=ticket.tune["hz"], gain=ticket.tune["gain"])
        try:
            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            while self._runs.is_current(number) and not ticket.cancelled.is_set():
                if not select.select([fd], [], [], 0.05)[0]:
                    continue
                try:
                    data = os.read(fd, min(65_536, BYTES + 1 - ticket.count))
                except BlockingIOError:
                    continue
                with self._runs:
                    if not self._runs.is_current(number):
                        return
                    if not data:
                        ticket.eof = True
                        return
                    if ticket.arrived_at is None:
                        ticket.arrived_at = _utc()
                    ticket.data.extend(data)
                    ticket.count += len(data)
                    if ticket.count > BYTES:
                        ticket.read_error = "Recorder sent more than the capture limit"
                        break
        except (OSError, ValueError) as exc:
            ticket.read_error = f"Cannot read recorder samples: {exc}"
        with self._life:
            run = self._run
            if ticket.read_error and run and run.number == number and not run.reaped:
                self._cancel_recording(ticket, "error", ticket.read_error)
                run.cancel.set()
                run.wake.set()

    def _outcome(self, run, ticket):
        if ticket.cancelled.is_set():
            return ticket.state, ticket.reason
        if run.cancel.is_set() and not run.preserve_drain:
            return "stopped", "Capture interrupted by another instrument"
        if ticket.read_error or run.diagnostic_fault:
            return "error", ticket.read_error or run.diagnostic_fault
        try:
            fd = run.proc.stderr.fileno()
            head = os.pread(fd, DIAG_BYTES, 0).decode("utf-8", "replace")
            tail = os.pread(fd, DIAG_BYTES, max(0, os.fstat(fd).st_size - DIAG_BYTES))
            diagnostics = head + tail.decode("utf-8", "replace")
        except (OSError, ValueError) as exc:
            return "error", f"Capture exit diagnostics could not be read: {exc}"
        if _USB_FAILURE.search(diagnostics):
            return "error", "The radio stopped answering"
        if _TUNER_FAILURE.search(diagnostics):
            return "error", "The recorder could not set up the tuner"
        if not _matches_radio(head, ticket.radio, ticket.census_size):
            return "error", "The radio changed while opening"
        if run.exit_code != 0:
            return "error", f"The recorder exited with status {run.exit_code}"
        if not ticket.eof or ticket.count != BYTES:
            return "error", "The recorder did not return the complete sample stream"
        return "saving", None

    def _report(self, run):
        ticket, = run.args
        with self._life:
            ticket.timer.cancel()
            ticket.deadline = None

        def admit():
            with self._life:
                if ticket.cancelled.is_set():
                    return False
                ticket.commit_admitted = True
                return True

        saved, state, reason = None, "error", "Capture report failed"
        try:
            state, reason = self._outcome(run, ticket)
            if state == "saving":
                with self._life:
                    if not ticket.cancelled.is_set():
                        ticket.state = "saving"
                saved = self.store.save(ticket.data, _sigmf(ticket), admit)
                state, reason = ("saved", None) if saved else (ticket.state, ticket.reason)
        except (CaptureStorageError, ValueError) as exc:
            state, reason = "error", str(exc)
        finally:
            with self._life:
                if ticket.cancelled.is_set():
                    state, reason = ticket.state, ticket.reason
                if saved is not None:
                    self._last_saved = saved
                ticket.data.clear()
                ticket.state, ticket.reason = state, reason
                run.unreported_death = False
                self.error = None
                self.watch.superseded(ticket.tune["hz"])
            try:
                if saved is not None:
                    self._event("capture_saved", ticket, **saved)
                self._event("capture_end", ticket, state=state, reason=reason)
            finally:
                with self._life:
                    ticket.busy = False

    def protection(self):
        """Seconds left for an opening or recording receiver, without probing any process."""
        with self._life:
            ticket = self._ticket
            if not ticket or not ticket.busy:
                return None
            if self._run and self._run.args[0] is ticket and self._run.reaped:
                return None
            return (CAPTURE_TIMEOUT_S if ticket.deadline is None
                    else max(0.01, ticket.deadline - time.monotonic()))

    @property
    def running(self):
        # Until C40 publishes physical completion the receiver may still own the dongle.
        run = self._run
        return run is not None and not run.reaped

    def snapshot(self, confirmed=None):
        with self._life:
            ticket = self._ticket
            pending = self.release_failure(pending=True)
            reason = ticket.reason if ticket else None
            if pending:
                reason = f"{reason}. {pending}" if reason and pending != reason else pending
            return {
                "state": ticket.state if ticket else "idle",
                "run_id": ticket.id if ticket else None,
                "confirmed": confirmed, "tune": ticket.tune if ticket else None,
                "bytes": ticket.count if ticket else 0, "byte_limit": BYTES,
                "remaining": (max(0, ticket.deadline - time.monotonic())
                              if ticket and ticket.deadline is not None else None),
                "reason": reason,
                "finishing": bool(ticket and ticket.busy and ticket.commit_admitted),
                "last_saved": self._last_saved,
            }
