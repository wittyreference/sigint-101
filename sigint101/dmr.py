# ABOUTME: Decodes DMR/P25 digital-voice signaling via dsd-fme for M27 — who is talking, by ID.
# ABOUTME: No radio, no dsd-fme, no traffic — reports an empty log, never invents a call.

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass

from .hardware import (
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# dsd-fme's binary (the DSD-FME fork, github.com/lwvmobile/dsd-fme). Named here rather than in
# hardware.py so the parallel decoder builds do not collide on a shared constant.
DSD_FME = "dsd-fme"

# Where the DMR/P25 lesson listens. 450 MHz sits in the UHF band that carries most amateur and
# business digital-voice repeaters; a pack can retune via tune. dsd-fme tunes here through its own
# RTL-SDR front end.
DEFAULT_DMR_HZ = 450_000_000

# How many recent calls the log keeps. Digital voice is a stream of one-off transmissions, not a set
# of stations, so this is a bounded newest-first history, like the ACARS and pager logs.
DEFAULT_LOG_LEN = 60

# dsd-fme's RTL-SDR demodulation bandwidth in kHz (8/12/16/24; 12 is its default). The input
# selector is rtl:dev:freq:gain:ppm:bw[:sq:vol]; gain 0 leaves the tuner on hardware AGC.
DSD_BANDWIDTH_KHZ = 12

# dsd-fme prints "<time> Sync: <frametype> " at each sync detection (src/dsd_frame_sync.c); the
# frametype carries the protocol (+DMR, -DMR, +P25p1, +P25p2, ...). The protocol decoder appends the
# call metadata to that same line, so one line carries the whole event.
_SYNC = re.compile(r"Sync:\s*([+-]\S+)")

# The leading timestamp printFrameSync prints before "Sync:".
_TIME = re.compile(r"^\s*(\d{1,2}:\d{2}:\d{2})")

# The DMR colour code, printed as "Color Code=NN". A DMR concept only; P25 has none.
_COLOR_CODE = re.compile(r"Color Code=(\d+)")

# The active DMR time-slot, printed as "SLOT N" on the voice line.
_SLOT = re.compile(r"\bSLOT\s+(\d+)", re.IGNORECASE)

# The talkgroup / target radio ID. dsd-fme labels it TGT= (DMR voice LC), TG:/SG: (updates), or
# "Group N" / "Target N" (P25 LCW, src/p25_lcw.c). For a private call it holds the called unit.
_TALKGROUP = re.compile(r"\b(?:TGT|TG|Target|Group|SG)\b\s*[=:]?\s*(\d+)", re.IGNORECASE)

# The source radio ID. Labelled SRC= (DMR) or "Source N" (P25 LCW).
_SOURCE = re.compile(r"\b(?:SRC|Source)\b\s*[=:]?\s*(\d+)", re.IGNORECASE)


class DmrUnavailable(RuntimeError):
    """The DMR/P25 stream cannot be read. Never a reason to invent a call — the log stays empty."""


@dataclass(frozen=True)
class DmrEvent:
    """One decoded digital-voice call event: who keyed up, on what talkgroup, in what protocol."""

    protocol: str              # "DMR" or "P25"
    talkgroup: int | None      # talkgroup, or the target unit on a private/unit-to-unit call
    source: int | None         # the transmitting radio's ID
    slot: int | None           # DMR time-slot (1 or 2); None for P25
    color_code: int | None     # DMR colour code (0-15); None for P25
    kind: str                  # call type: "group", "private", or "voice"
    last_seen: str | None      # the HH:MM:SS dsd-fme stamped on the frame

    def as_dict(self) -> dict:
        return {
            "protocol": self.protocol,
            "talkgroup": self.talkgroup,
            "source": self.source,
            "slot": self.slot,
            "color_code": self.color_code,
            "kind": self.kind,
            "last_seen": self.last_seen,
        }


def _protocol(sync_token: str) -> str | None:
    """DMR or P25 from a dsd-fme sync frametype, or None for a mode this lesson does not cover."""
    token = sync_token.upper()
    if "DMR" in token:
        return "DMR"
    if "P25" in token:
        return "P25"
    return None


def _classify(line: str) -> str:
    """The call type from the line's own words: a private/unit-to-unit, a group call, or voice."""
    if "Private" in line or "Unit to Unit" in line:
        return "private"
    if "Group" in line:
        return "group"
    return "voice"


def _as_int(match):
    return int(match.group(1)) if match else None


def parse_dsd_line(line: str) -> DmrEvent | None:
    """Turn one dsd-fme output line into a DmrEvent, or None if it is not a decodable call.

    A call event needs a sync (to name the protocol) and at least one radio ID — a talkgroup/target
    or a source. A bare sync with no IDs, a control frame whose IDs are on another line, an
    unsupported mode, and any non-dsd line all carry no call to log, so they return None.
    """
    text = (line or "").strip()
    if not text:
        return None
    sync = _SYNC.search(text)
    if not sync:
        return None
    protocol = _protocol(sync.group(1))
    if protocol is None:
        return None
    talkgroup = _as_int(_TALKGROUP.search(text))
    source = _as_int(_SOURCE.search(text))
    if talkgroup is None and source is None:
        return None
    time_match = _TIME.match(text)
    return DmrEvent(
        protocol=protocol,
        talkgroup=talkgroup,
        source=source,
        slot=_as_int(_SLOT.search(text)),
        color_code=_as_int(_COLOR_CODE.search(text)),
        kind=_classify(text),
        last_seen=time_match.group(1) if time_match else None,
    )


class CallLog:
    """A bounded, newest-first history of decoded DMR/P25 call events."""

    def __init__(self, maxlen: int = DEFAULT_LOG_LEN):
        self._calls = deque(maxlen=maxlen)

    def add(self, event: DmrEvent | None) -> None:
        if event is not None:
            self._calls.append(event)

    def recent(self, limit: int | None = None) -> list:
        """The most recent calls, newest first, optionally capped at `limit`."""
        calls = list(reversed(self._calls))
        return calls[:limit] if limit else calls


class _DsdProcess:
    """The dsd-fme process as the reader wants it: its decoded stderr stream exposed as .stdout.

    dsd-fme writes its decoded signaling to stderr and keeps stdout for audio (sent to the null
    sink), so the reader reads stderr. Wrapping it this way keeps the reader identical to the other
    decoders', which iterate `self._proc.stdout`.
    """

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self.stdout = proc.stderr

    def poll(self):
        return self._proc.poll()

    def terminate(self):
        try:
            self._proc.terminate()
        except (OSError, subprocess.SubprocessError):
            pass

    def wait(self, timeout=None):
        try:
            return self._proc.wait(timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return 0


def _popen(freq_hz: int, device: int):
    """Launch dsd-fme auto-detecting DMR/P25 on the RTL-SDR, decoded signaling flowing to stderr."""
    rtl = f"rtl:{device}:{freq_hz}:0:0:{DSD_BANDWIDTH_KHZ}"
    proc = subprocess.Popen(
        [DSD_FME, "-fa", "-i", rtl, "-o", "null"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return _DsdProcess(proc)


class DmrDecoder:
    """Owns one dsd-fme process and the log of DMR/P25 calls it decodes.

    A sibling of the ACARS decoder: an SDR-exclusive tool with a reader thread off the request path
    and release/resume so it takes turns with the waterfall on the one dongle. Like dump1090 and
    acarsdec it opens the radio itself, so it needs no rtl_fm front end.
    """

    def __init__(self, *, freq_hz=DEFAULT_DMR_HZ, device=0, spawn=_popen,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None):
        self.freq_hz = int(freq_hz)
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._log = CallLog()
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
            raise DmrUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed."""
        with self._life:
            self._released = True
            self.stop()
            with self._lock:
                self._log = CallLog()

    def resume(self) -> None:
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin decoding DMR/P25, unless the radio is absent, dsd-fme is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        if self._released:
            raise DmrUnavailable(
                "the radio was released to another program; resume DMR/P25 to take it back")
        if not self._which(DSD_FME):
            raise DmrUnavailable(
                f"{DSD_FME} is not installed, so SIGINT 101 cannot decode DMR or P25")
        if not self._radio_present():
            raise DmrUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a call")
        if self.error:
            reason, self.error = self.error, None
            raise DmrUnavailable(reason)
        self.device = self._choose_device()
        try:
            self._proc = self._spawn(self.freq_hz, self.device)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise DmrUnavailable(f"cannot run dsd-fme: {exc}") from exc
        self.error = None
        self._thread = threading.Thread(target=self._read, name="sigint101-dmr", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Consume dsd-fme's decoded lines on a background thread, logging calls until it ends."""
        try:
            for raw in self._proc.stdout:
                event = parse_dsd_line(raw)
                if event is not None:
                    with self._lock:
                        self._log.add(event)
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
                self.error = "dsd-fme did not stop; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. A real DMR/P25 stream never ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def calls(self, limit: int | None = None) -> list:
        """The recent calls as plain dicts for the API, newest first."""
        with self._lock:
            rows = self._log.recent(limit)
        return [c.as_dict() for c in rows]
