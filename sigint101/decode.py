# ABOUTME: Drives rtl_433 and turns its JSON decode lines into sensor reads for the M10 FSK lesson.
# ABOUTME: No radio, no rtl_433, no signal — this module reports absence, it never invents a decode.

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass

from .childproc import (
    ChildWatch,
    RunGuard,
    command_line,
    spawn,
)
from .hardware import (
    RTL_433,
    DeviceSelectionError,
    probe_rtlsdr,
    rtlsdr_radio_for_run,
)
from .receiver_lifecycle import SupervisedReceiver

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


class Decoder(SupervisedReceiver):
    """Owns one supervised rtl_433 process and a bounded buffer of recent sensor reads.

    Uses the shared C40 receiver lifecycle (background supervisor, off-lock preflight and reap
    confirmation, release/resume turn-taking on the single dongle) while keeping its own
    JSON-line parsing, repeat filtering, and bounded read history.
    """

    _unavailable = DecodeUnavailable
    _released_reason = "the radio was released to another program; resume decoding to take it back"

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
        # the run guard. The console's hook logs the run's command, frequency and radio with
        # each reception and never raises. A sensor's repeated copies of one transmission
        # report once (RepeatFilter); the panel's buffer keeps every copy printed.
        self.on_reception = None
        self._repeats = RepeatFilter()
        self._runs = RunGuard()
        self._init_lifecycle()
        self.error = None
        # Who ended the child, and when the console may launch it again after it died on its own.
        self.watch = ChildWatch(RTL_433)

    def _clear(self) -> None:
        self._reads.clear()

    def _choose_radio(self) -> dict:
        """Which radio to open, re-checked every start (the bus renumbers under us)."""
        try:
            return rtlsdr_radio_for_run(self.device, self.serial, self._list_devices)
        except DeviceSelectionError as exc:
            raise DecodeUnavailable(str(exc)) from exc

    @staticmethod
    def _build_command(freq_hz: int, device: int) -> list:
        return [RTL_433,
                "-f", str(freq_hz),
                "-F", "json",
                "-M", "level",
                "-d", str(device)]

    def command(self) -> list:
        """The rtl_433 invocation: one centre frequency, JSON out, level metadata for SNR."""
        return self._build_command(self.freq_hz, self.device)

    def _prepare_launch(self, freq_hz, _number):
        if not self._which(RTL_433):
            raise DecodeUnavailable(
                f"{RTL_433} is not installed, so SIGINT 101 cannot decode the ISM band")
        if not self._radio_present():
            raise DecodeUnavailable(
                "no RTL-SDR on the USB bus — SIGINT 101 will not fake a decode")
        radio = self._choose_radio()
        argv = tuple(self._build_command(freq_hz, radio["index"]))

        def launch():
            try:
                proc = self._spawn(list(argv))
            except (OSError, ValueError) as exc:
                raise DecodeUnavailable(f"cannot run {RTL_433}: {exc}") from exc
            self.device = radio["index"]
            return proc

        return launch, (freq_hz, radio)

    def _read(self, proc, run: int, freq_hz: int, radio: dict) -> None:
        """Consume rtl_433's JSON on a background thread until the process ends."""
        cmd = command_line(proc)
        try:
            for raw in proc.stdout:
                decode = parse_decode_line(raw)
                if decode is None:
                    continue
                with self._runs:
                    if not self._runs.is_current(run):
                        continue
                    self._reads.append(decode)
                    repeat = self._repeats.is_repeat(decode, time.monotonic())
                    report = None if repeat else self._admit()
                if report is not None:
                    self._emit(report, decode.as_dict(), command=cmd, freq_hz=freq_hz,
                               radio=radio)
        except (OSError, ValueError) as exc:  # loud, never silent
            with self._runs:
                if self._runs.is_current(run):
                    self.error = str(exc)

    def recent(self, limit: int | None = None) -> list:
        """The most recent reads, oldest first. `limit` keeps only the newest that many."""
        with self._runs:
            reads = list(self._reads)
        if limit is not None:
            reads = reads[-limit:]
        return reads
