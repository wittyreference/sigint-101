# ABOUTME: Decodes ACARS via acarsdec for M16 — the typed messages aircraft send on VHF, in words.
# ABOUTME: No radio, no acarsdec, no messages — reports an empty log, never invents a transmission.

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass

from .hardware import (
    ACARSDEC,
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# Where the ACARS lesson listens. 131.550 MHz is the primary VHF ACARS channel worldwide; acarsdec
# can watch several at once, but one is enough to teach the idea, and a pack can retune via tune.
DEFAULT_ACARS_HZ = 131_550_000

# How many recent messages the log keeps. ACARS is a stream of one-off transmissions, not a set of
# stations, so this is a bounded newest-first history, like the pager log.
DEFAULT_LOG_LEN = 60


class AcarsUnavailable(RuntimeError):
    """The ACARS stream cannot be read. Never a reason to invent a message — the log stays empty."""


@dataclass(frozen=True)
class AcarsMessage:
    """One decoded ACARS transmission: which aircraft sent it, and what it said."""

    tail: str | None          # aircraft registration (e.g. F-GTAE)
    flight: str | None        # flight number (e.g. AF7728)
    label: str                # the message label (H1, Q0, 5V, ...)
    mode: str | None
    block_id: str | None
    msgno: str | None
    ack: object               # acarsdec reports the ack as false or the ack character
    text: str | None          # the free-text body, when the message carries one
    freq_mhz: float | None
    level: float | None

    def as_dict(self) -> dict:
        return {
            "tail": self.tail,
            "flight": self.flight,
            "label": self.label,
            "mode": self.mode,
            "block_id": self.block_id,
            "msgno": self.msgno,
            "ack": self.ack,
            "text": self.text,
            "freq_mhz": self.freq_mhz,
            "level": self.level,
        }


def parse_acars_message(line: str) -> AcarsMessage | None:
    """Turn one acarsdec JSON line into an AcarsMessage, or None if it is not one.

    acarsdec prints one JSON object per decoded message with '-o 4'. Every ACARS message carries a
    `label`; a line that is not a JSON object, or an object without a label, is not a message. The
    free-text body is optional — a link test or ack carries none.
    """
    text = (line or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "label" not in obj:
        return None
    msg_text = obj.get("text")
    if msg_text is not None:
        msg_text = str(msg_text).rstrip() or None
    return AcarsMessage(
        tail=obj.get("tail") or None,
        flight=obj.get("flight") or None,
        label=str(obj["label"]),
        mode=obj.get("mode"),
        block_id=obj.get("block_id"),
        msgno=obj.get("msgno"),
        ack=obj.get("ack"),
        text=msg_text,
        freq_mhz=obj.get("freq"),
        level=obj.get("level"),
    )


class AcarsLog:
    """A bounded, newest-first history of decoded ACARS messages."""

    def __init__(self, maxlen: int = DEFAULT_LOG_LEN):
        self._msgs = deque(maxlen=maxlen)

    def add(self, msg: AcarsMessage | None) -> None:
        if msg is not None:
            self._msgs.append(msg)

    def recent(self, limit: int | None = None) -> list:
        """The most recent messages, newest first, optionally capped at `limit`."""
        msgs = list(reversed(self._msgs))
        return msgs[:limit] if limit else msgs


def _popen(freq_hz: int, device: int):
    """Launch acarsdec on the VHF ACARS channel, printing decoded messages as JSON to stdout."""
    freq_mhz = f"{freq_hz / 1e6:.3f}"
    return subprocess.Popen(
        [ACARSDEC, "-o", "4", "-g", "49.6", "-r", str(device), freq_mhz],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


class AcarsDecoder:
    """Owns one acarsdec process and the log of messages it decodes.

    A sibling of the POCSAG decoder: an SDR-exclusive tool with a reader thread off the request path
    and release/resume so it takes turns with the waterfall on the one dongle. Unlike the multimon
    lessons it needs no rtl_fm front end — acarsdec opens the radio itself, like dump1090.
    """

    def __init__(self, *, freq_hz=DEFAULT_ACARS_HZ, device=0, spawn=_popen,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None):
        self.freq_hz = int(freq_hz)
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._log = AcarsLog()
        self._lock = threading.Lock()
        self._life = threading.RLock()
        self._proc = None
        self._thread = None
        self._released = False
        self.error = None

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def released(self) -> bool:
        return self._released

    def _choose_device(self) -> int:
        if self._list_devices is None:
            return self.device
        try:
            return select_rtlsdr_index(self.serial, devices=self._list_devices())
        except DeviceSelectionError as exc:
            raise AcarsUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed."""
        with self._life:
            self._released = True
            self.stop()
            with self._lock:
                self._log = AcarsLog()

    def resume(self) -> None:
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin decoding ACARS, unless the radio is absent, acarsdec is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        if self._released:
            raise AcarsUnavailable(
                "the radio was released to another program; resume ACARS to take it back")
        if not self._which(ACARSDEC):
            raise AcarsUnavailable(
                f"{ACARSDEC} is not installed, so SIGINT 101 cannot decode ACARS")
        if not self._radio_present():
            raise AcarsUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a message")
        if self.error:
            reason, self.error = self.error, None
            raise AcarsUnavailable(reason)
        self.device = self._choose_device()
        try:
            self._proc = self._spawn(self.freq_hz, self.device)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise AcarsUnavailable(f"cannot run acarsdec: {exc}") from exc
        self.error = None
        self._thread = threading.Thread(target=self._read, name="sigint101-acars", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Consume acarsdec's JSON on a background thread, logging messages until it ends."""
        try:
            for raw in self._proc.stdout:
                msg = parse_acars_message(raw)
                if msg is not None:
                    with self._lock:
                        self._log.add(msg)
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
                self.error = "acarsdec did not stop; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. A real ACARS stream never ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def messages(self, limit: int | None = None) -> list:
        """The recent messages as plain dicts for the API, newest first."""
        with self._lock:
            rows = self._log.recent(limit)
        return [m.as_dict() for m in rows]
