# ABOUTME: Drives rtl_433 and turns its JSON decode lines into sensor reads for the M10 FSK lesson.
# ABOUTME: No radio, no rtl_433, no signal — this module reports absence, it never invents a decode.

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass

from .hardware import (
    RTL_433,
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
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


def _popen(cmd):
    """Launch rtl_433 so its JSON can be read line by line as devices transmit.

    stderr goes to a temporary file, not a pipe: nothing drains it while the decoder runs, and
    a pipe whose buffer fills would wedge rtl_433 rather than the diagnostic. The reason a dead
    rtl_433 gives ("No supported devices found.") is there to read when a start fails.
    """
    scratch = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=scratch, text=True)
    proc.stderr = scratch
    return proc


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
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._reads = deque(maxlen=history)
        # Two locks, same discipline as the sweeper: _lock guards the buffer the reader writes,
        # _life guards start/stop/release so two request threads cannot race over the dongle.
        self._lock = threading.Lock()
        self._life = threading.RLock()
        self._proc = None
        self._thread = None
        self._released = False
        self.error = None

    @property
    def running(self) -> bool:
        """True while an rtl_433 process is alive and holding the dongle."""
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def released(self) -> bool:
        """True while the dongle has been handed to another program on purpose."""
        return self._released

    def _choose_device(self) -> int:
        """Which radio index to open, re-checked every start (the bus renumbers under us)."""
        if self._list_devices is None:
            return self.device
        try:
            return select_rtlsdr_index(self.serial, devices=self._list_devices())
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

    def start(self) -> None:
        """Begin decoding, unless the radio is absent, rtl_433 is missing, or we were released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
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
        self.device = self._choose_device()
        try:
            self._proc = self._spawn(self.command())
        except (OSError, ValueError) as exc:
            self._proc = None
            raise DecodeUnavailable(f"cannot run {RTL_433}: {exc}") from exc
        self.error = None
        self._thread = threading.Thread(target=self._read, name="sigint101-decoder", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Consume rtl_433's JSON on a background thread until the process ends."""
        try:
            for raw in self._proc.stdout:
                decode = parse_decode_line(raw)
                if decode is not None:
                    with self._lock:
                        self._reads.append(decode)
        except (OSError, ValueError) as exc:  # loud, never silent
            self.error = str(exc)

    def stop(self) -> None:
        """End decoding and let go of the dongle."""
        with self._life:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.error = "rtl_433 survived kill; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

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
