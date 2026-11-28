# ABOUTME: Drives rtl_433 and turns its JSON decode lines into sensor reads for the M10 FSK lesson.
# ABOUTME: No radio, no rtl_433, no signal — this module reports absence, it never invents a decode.

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass

from . import childproc
from .childproc import (
    ChildWatch,
    command_line,
    describe_exits,
    emit_reception,
    running_command,
    spawn,
    stop_process,
)
from .hardware import (
    RTL_433,
    DeviceSelectionError,
    probe_rtlsdr,
    rtlsdr_radio_for_run,
)

# The 433.92 MHz ISM band, where keyfobs, doorbells and the weather/sensor zoo chatter. rtl_433
# tunes to one centre frequency (it demodulates a band around it) rather than sweeping a span.
DEFAULT_DECODE_HZ = 433_920_000

# How many recent reads to keep. A handful is enough for the panel; the point is the latest
# sensor speaking, not a log.
DEFAULT_HISTORY = 40

# Keys that describe the transmission, not the sensor's message. Everything else in a decode
# line is a reading the device actually reported (temperature, humidity, id, channel, ...).
_ENVELOPE_KEYS = frozenset({"time", "model", "mod", "freq", "freq1", "freq2",
                            "rssi", "snr", "noise"})

# rtl_433 prints every copy of a packet a sensor repeats: an Acurite-Tower sends each reading three
# times in one burst, and all three decode (captured on the reference Recon: three identical
# lines inside 6 ms, the next reading ~17 s later). Copies of one transmission land milliseconds
# apart and a sensor's next reading seconds later, so a copy inside this window is the same
# reception.
REPEAT_WINDOW_S = 2.0

# The refusal for an rtl_433 that a stop could not end. It is the radio's state, not the run's,
# so a retune keeps it.
SURVIVED_KILL = f"{RTL_433} survived kill; the dongle may still be held"


def _unconfirmed(exc) -> str:
    return f"{RTL_433} could not be confirmed stopped ({exc}); the dongle may still be held"


def _kill_and_confirm(proc) -> str | None:
    """SIGKILL a retiring child that is still ours, then wait for its reap.

    Returns None once its exit is confirmed, else why not. The signal goes to the kept process
    object only after poll() shows it still unreaped, so a reaped child's PID, which may be
    another process's by now, is never signalled. An error in the poll, the signal or the wait
    confirms nothing.
    """
    try:
        if proc.poll() is not None:
            return None
    except Exception as exc:
        return _unconfirmed(exc)                # cannot tell it is still ours: no signal
    try:
        proc.kill()
    except OSError:
        pass                                    # gone already; the wait says
    except Exception as exc:
        return _unconfirmed(exc)
    try:
        proc.wait(timeout=childproc.STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        return SURVIVED_KILL
    except Exception as exc:
        return _unconfirmed(exc)
    return None


class DecodeUnavailable(RuntimeError):
    """The radio cannot be decoded. Never a reason to synthesise a read — the panel says so."""


@dataclass(frozen=True)
class Decode:
    """One sensor transmission rtl_433 recognised and read.

    `freq2_hz` is the second tone and is set only for FSK — the mark/space pair that is the
    whole shape of the lesson. ASK rides a single frequency switched on and off, so it is None.
    `readings` is the device's actual message (temperature, humidity, ...), with the
    transmission envelope stripped out.
    """

    ts: str
    model: str
    mod: str
    freq_hz: int
    freq2_hz: int | None
    snr_db: float | None
    readings: dict

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "model": self.model,
            "mod": self.mod,
            "freq_hz": self.freq_hz,
            "freq2_hz": self.freq2_hz,
            "snr_db": self.snr_db,
            "readings": self.readings,
        }


def _to_hz(mhz) -> int | None:
    """rtl_433 reports frequencies in MHz; the console speaks Hz."""
    try:
        return round(float(mhz) * 1_000_000)
    except (TypeError, ValueError):
        return None


def parse_decode_line(raw: str) -> Decode | None:
    """Turn one rtl_433 `-F json` line into a Decode, or None if it is not a sensor read.

    rtl_433 interleaves narration ("Tuned to 433.920MHz.") and, with some flags, non-decode
    JSON; neither is a message from a device. A line is a read only when it parses as a JSON
    object carrying a `model`. Anything else returns None rather than a blank-fielded decode.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "model" not in obj:
        return None

    # FSK carries freq1/freq2 (the two tones); ASK carries a single freq. Prefer freq1 so the
    # primary tone is always freq_hz, and expose freq2 only when the signal actually shifts.
    freq_hz = _to_hz(obj.get("freq1", obj.get("freq")))
    freq2_hz = _to_hz(obj["freq2"]) if "freq2" in obj else None
    try:
        snr_db = float(obj["snr"]) if obj.get("snr") is not None else None
    except (TypeError, ValueError):
        snr_db = None

    readings = {k: v for k, v in obj.items() if k not in _ENVELOPE_KEYS}
    return Decode(
        ts=str(obj.get("time", "")),
        model=str(obj["model"]),
        mod=str(obj.get("mod", "")),
        freq_hz=freq_hz if freq_hz is not None else 0,
        freq2_hz=freq2_hz,
        snr_db=snr_db,
        readings=readings,
    )


class RepeatFilter:
    """Tells a sensor's repeated copies of one transmission from a reading it has not sent before.

    A decode is a copy when its model and every reading (the id included) match one heard less
    than the window ago; the envelope (signal level, frequency estimate, timestamp) is left out.
    Changed values, another sensor, or the same values after the window are new receptions.
    """

    def __init__(self, window_s: float = REPEAT_WINDOW_S):
        self.window_s = window_s
        self._heard = {}   # (model, readings) -> when the copy that was reported arrived

    def is_repeat(self, decode: Decode, now: float) -> bool:
        self._heard = {key: at for key, at in self._heard.items() if now - at < self.window_s}
        key = (decode.model, json.dumps(decode.readings, sort_keys=True, default=str))
        if key in self._heard:
            return True
        self._heard[key] = now
        return False


def _popen(cmd):
    """Launch rtl_433 so its JSON can be read line by line as devices transmit.

    stderr goes to a temporary file, not a pipe: nothing drains it while the decoder runs, and
    a pipe whose buffer fills would wedge rtl_433 rather than the diagnostic. The reason a dead
    rtl_433 gives ("No supported devices found.") is there to read when a start fails.
    spawn() runs it in its own session, tied to the console's life.
    """
    return spawn(cmd, stdout=subprocess.PIPE, text=True)


class Decoder:
    """Owns one long-lived rtl_433 process and a bounded buffer of recent sensor reads.

    Structurally a sibling of Sweeper: one process (opening the dongle costs seconds), a reader
    thread off the request path, and release/resume so the decoder and the waterfall take turns
    on the single radio instead of fighting over it.
    """

    def __init__(self, *, freq_hz=DEFAULT_DECODE_HZ, device=0, history=DEFAULT_HISTORY,
                 spawn=_popen, radio_present=None, which=shutil.which,
                 serial=None, list_devices=None):
        self.freq_hz = int(freq_hz)
        # The frequency this decoder was built for; the console tunes a lesson back to it.
        self.home_hz = self.freq_hz
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._reads = deque(maxlen=history)
        # Called with each sensor read as a plain dict, from the reader thread only and outside
        # the buffer lock. The console's hook logs the run's command, frequency and radio with
        # each reception and never raises. A sensor's repeated copies of one transmission
        # report once (RepeatFilter); the panel's buffer keeps every copy printed.
        self.on_reception = None
        self._repeats = RepeatFilter()
        # Two locks, same discipline as the sweeper: _lock guards the buffer the reader writes, and
        # which run may write to it; _life guards start/stop/release so two request threads cannot
        # race over the dongle.
        self._lock = threading.Lock()
        self._life = threading.RLock()
        self._proc = None
        self._thread = None
        # The rtl_433 a stop is retiring, kept from the moment the stop takes it until its exit is
        # confirmed: while the stop waits, and after a stop that could not confirm it. Until then
        # it may still hold the dongle, so every start and every handoff is refused
        # (release_failure). _retire_failure says why once the stop has given up.
        self._retiring = None
        self._retire_failure = None
        # The run a reader may list, report and record into. start() hands it to the reader it
        # starts; every stop moves it on, so a reader that outlives its stop is shut out (_read).
        self._run = 0
        self._released = False
        self.error = None
        # Who ended the child, and when the console may launch it again after it died on its own.
        self.watch = ChildWatch(RTL_433)

    @property
    def running(self) -> bool:
        """True while an rtl_433 process is alive and holding the dongle."""
        proc = self._proc
        return proc is not None and proc.poll() is None

    def live_command(self) -> str | None:
        """The exact command running now, read off the live process; None when nothing runs."""
        return running_command(self._proc)

    @property
    def released(self) -> bool:
        """True while the dongle has been handed to another program on purpose."""
        return self._released

    def _choose_radio(self) -> dict:
        """Which radio to open, re-checked every start (the bus renumbers under us)."""
        try:
            return rtlsdr_radio_for_run(self.device, self.serial, self._list_devices)
        except DeviceSelectionError as exc:
            raise DecodeUnavailable(str(exc)) from exc

    def command(self) -> list:
        """The rtl_433 invocation: one centre frequency, JSON out, level metadata for SNR."""
        return [RTL_433,
                "-f", str(self.freq_hz),
                "-F", "json",
                "-M", "level",
                "-d", str(self.device)]

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed."""
        with self._life:
            self._released = True
            self.stop()
            with self._lock:
                self._reads.clear()

    def resume(self) -> None:
        """Take the radio back after another program has finished with it."""
        with self._life:
            self._released = False

    def retune(self, freq_hz) -> None:
        """Move to another frequency: the running child stops, and the next start() spawns there.

        The reads heard on the old frequency are cleared as release() clears them, but the dongle
        is not handed away. Tuning to where the decoder already is changes nothing.

        The stopped run's death or pipe failure is that run's, not the next run's answer, so it
        is dropped; the stop has moved the run on and marked the watch stopping first, so the old
        reader can record neither again. An rtl_433 the stop could not confirm stopped may still
        hold the dongle, and that refusal stands. The watch keeps a grown back-off
        (ChildWatch.superseded).
        """
        with self._life:
            if int(freq_hz) == self.freq_hz:
                return
            if self._proc is not None:
                self._count_visible_exit(self._proc)
            self._stop_locked()
            self.freq_hz = int(freq_hz)
            with self._lock:
                self._reads.clear()
                if self.error != self._retire_failure:
                    self.error = None
            self.watch.superseded(self.freq_hz)

    def _count_visible_exit(self, proc) -> None:
        """Count a child's exit that already shows, before the stop makes its reader's record stale.

        A child that has exited but whose reader has not seen its pipe close would otherwise never
        advance the back-off. Held under _life, so the death is counted from its exit status and
        the child's identity only: no stderr is read and nothing is waited on, and a stalled
        diagnostic cannot hold the stand-down a handoff needs. The tool's own words are not
        needed here, as the retune replaces the old death's reason anyway. ChildWatch commits one
        death per child, so the reader recording the same exit cannot count it twice.
        """
        try:
            code = proc.poll()
        except (OSError, ValueError):
            return
        if code is not None:
            reason = describe_exits([(proc, code)], tail=(), tool=RTL_433)
            self.watch.observed(proc, reason, self.watch.clock())

    def start(self) -> None:
        """Begin decoding, unless the radio is absent, rtl_433 is missing, or we were released."""
        with self._life:
            self._start_locked()

    def release_failure(self, *, pending=False) -> str | None:
        """Why the radio cannot be handed on: a retiring rtl_433's exit is not confirmed yet.

        The console asks every receiver this before a handoff, and the hardware status asks it
        with `pending`, as they ask the supervised receivers. A stop that gave up says why. A
        stop still waiting on the reap says so only when `pending` is asked; a plain check would
        wait for that stop anyway, as it holds the decoder.
        """
        proc = self._retiring
        if proc is None:
            return None
        try:
            alive = proc.poll() is None
        except Exception:
            alive = True                        # an exit that cannot be read is not confirmed
        if not alive:
            return None
        failure = self._retire_failure
        if failure is not None:
            return failure
        return f"{RTL_433} is still releasing the radio" if pending else None

    def _start_locked(self) -> None:
        if self.running:
            return
        self._settle()
        # Every start refuses while the old rtl_433 is alive, not just the first: the child and
        # its refusal are kept until its exit is confirmed.
        if self._retiring is not None:
            if reason := self.release_failure(pending=True):
                raise DecodeUnavailable(reason)
            failure, self._retiring, self._retire_failure = self._retire_failure, None, None
            with self._lock:
                if failure is not None and self.error == failure:
                    self.error = None
        if self._released:
            raise DecodeUnavailable(
                "the radio was released to another program; resume decoding to take it back")
        if not self._which(RTL_433):
            raise DecodeUnavailable(
                f"{RTL_433} is not installed, so SIGINT 101 cannot decode the ISM band")
        if not self._radio_present():
            raise DecodeUnavailable(
                "no RTL-SDR on the USB bus — SIGINT 101 will not fake a decode")
        if self.error:
            reason, self.error = self.error, None
            raise DecodeUnavailable(reason)
        # A child that died on its own is not relaunched on every poll: the reason stands, and the
        # console waits out a back-off that grows while the deaths keep coming.
        blocked = self.watch.blocked()
        if blocked:
            raise DecodeUnavailable(blocked)
        radio = self._choose_radio()
        self.device = radio["index"]
        try:
            self._proc = self._spawn(self.command())
        except (OSError, ValueError) as exc:
            self._proc = None
            raise DecodeUnavailable(f"cannot run {RTL_433}: {exc}") from exc
        self.watch.spawned(self._proc)
        self.error = None
        # Only a stop moves the run on, and stop and start both hold _life, so this is the run
        # now, and it stays this reader's until the next stop.
        self._thread = threading.Thread(target=self._read,
                                        args=(self._proc, self._run, command_line(self._proc),
                                              self.freq_hz, radio),
                                        name="sigint101-decoder", daemon=True)
        self._thread.start()

    def _read(self, proc, run: int, command: str | None, freq_hz: int, radio: dict) -> None:
        """Consume rtl_433's JSON on a background thread until the process ends.

        The reader can outlive its stop: an rtl_433 that is not reaped keeps the pipe open past
        stop()'s wait, and by then a retune may have the next run decoding on another frequency.
        So each read is listed and reported only while `run` is still the run now, tested under
        the buffer lock that every stop takes to move it on. After that the reader reads on to the
        end of its pipe, and lists, reports and records nothing.
        """
        try:
            for raw in proc.stdout:
                decode = parse_decode_line(raw)
                if decode is None:
                    continue
                with self._lock:
                    if run != self._run:
                        continue
                    self._reads.append(decode)
                    repeat = self._repeats.is_repeat(decode, time.monotonic())
                report = self.on_reception
                if report is not None and not repeat:
                    emit_reception(report, decode.as_dict(), command=command, freq_hz=freq_hz,
                                   radio=radio)
        except (OSError, ValueError) as exc:  # loud, never silent
            with self._lock:
                if run == self._run:
                    self.error = str(exc)
        finally:
            self._note_end(proc, run)

    def _note_end(self, proc, run: int) -> None:
        """Record why the child ended, if it was not us, so the next poll reports it as stopped.

        The reason is found outside the buffer lock, as finding it can wait for an exit status,
        and recorded only if `run` is still the run now, so it never becomes a later run's.
        """
        reason = self.watch.ended(proc)
        if reason:
            with self._lock:
                if run == self._run and self.error is None:
                    self.error = reason

    def _settle(self) -> None:
        """Account for a child that ended since the last poll, then clear away what it left."""
        if self._proc is not None:
            self._note_end(self._proc, self._run)
            self._stop_locked()

    #: How long stop() waits, in seconds, for the reader to end once rtl_433 is stopped. A reader
    #: can outlive the wait; it then lists, reports and records nothing (_read).
    STOP_WAIT_S = 3.0

    def stop(self) -> None:
        """End decoding and let go of the dongle."""
        with self._life:
            self._stop_locked()

    def _stop_locked(self) -> None:
        # The run ends first, under the buffer lock, so nothing its reader reads from here on is
        # listed, reported or recorded, however long that reader outlives this stop.
        with self._lock:
            self._run += 1
        # Marked before the kill, so the reader seeing the output end never reports our stop as a
        # death.
        self.watch.stopping()
        proc = self._proc
        if proc is not None:
            # Retiring from here: release_failure(pending=True) reports it while the stop waits.
            self._retire_failure = None
            self._retiring = proc
        self._proc = None
        if proc is not None:
            try:
                failure = None if stop_process(proc) else SURVIVED_KILL
            except Exception:
                # A wait that errs skipped the SIGKILL that follows it: escalate now, so a tool
                # that ignores SIGTERM is not left holding the radio.
                failure = _kill_and_confirm(proc)
            self._settle_retirement(failure)
        elif self._retiring is not None:
            # A child an earlier stop could not confirm: signal it again and retry its reap.
            self._settle_retirement(_kill_and_confirm(self._retiring))
        if self._thread is not None:
            self._thread.join(timeout=self.STOP_WAIT_S)
            self._thread = None

    def _settle_retirement(self, failure: str | None) -> None:
        """Drop the retiring child once its exit is confirmed; otherwise keep it, and say why."""
        if failure is None:
            prior, self._retiring, self._retire_failure = self._retire_failure, None, None
            if prior is not None and self.error == prior:
                self.error = None
        else:
            self._retire_failure = failure
            self.error = failure

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. A real decode stream never ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def recent(self, limit: int | None = None) -> list:
        """The most recent reads, oldest first. `limit` keeps only the newest that many."""
        with self._lock:
            reads = list(self._reads)
        if limit is not None:
            reads = reads[-limit:]
        return reads
