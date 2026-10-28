# ABOUTME: Decodes POCSAG pager traffic via rtl_fm|multimon-ng for M15 — messages not meant for you.
# ABOUTME: No radio, no multimon-ng, no pages — reports an empty log, never invents a message.

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass

from .hardware import (
    MULTIMON,
    RTL_FM,
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# Where the POCSAG lesson listens. 929.6 MHz sits in the US paging band; a pack can retune via tune.
DEFAULT_POCSAG_HZ = 929_600_000

# multimon-ng expects 16-bit signed mono at 22050 Hz on its raw input; rtl_fm is told to match.
POCSAG_SAMPLE_RATE = 22050

# Pagers run at three bit rates, and a channel can carry any of them; enable all so none is missed.
POCSAG_DEMODS = ("POCSAG512", "POCSAG1200", "POCSAG2400")

# How many recent pages the log keeps. A pager channel is a stream of one-off messages, not a set of
# stations, so this is a bounded history (newest shown first), not a per-address roster.
DEFAULT_LOG_LEN = 60

# One multimon-ng POCSAG line: "POCSAG<baud>: Address: <addr>  Function: <fn> [Alpha:|Numeric:|..]".
# The address is right-aligned (leading spaces) and is '-' when none decoded; a tone-only page has
# no content label. The format is multimon-ng's pocsag.c.
_LINE = re.compile(
    r"^POCSAG(?P<baud>\d+):\s*Address:\s*(?P<addr>\S+)\s+Function:\s*(?P<func>\S+)\s*"
    r"(?:(?P<label>Alpha|Numeric|Skyper):\s*(?P<text>.*))?$")


class PocsagUnavailable(RuntimeError):
    """The POCSAG stream cannot be read. Never a reason to invent a page — the log stays empty."""


@dataclass(frozen=True)
class PocsagPage:
    """One decoded pager message: who it was addressed to, and what it carried."""

    baud: int
    address: int | None
    function: int | None
    kind: str                  # alpha | numeric | skyper | tone
    text: str | None = None

    def as_dict(self) -> dict:
        return {
            "baud": self.baud,
            "address": self.address,
            "function": self.function,
            "kind": self.kind,
            "text": self.text,
        }


def _maybe_int(token: str):
    return None if token == "-" else int(token)


def parse_pocsag_line(line: str) -> PocsagPage | None:
    """Turn one multimon-ng POCSAG line into a PocsagPage, or None if it is not one.

    multimon-ng's other demodulators and its status output take other shapes; only a POCSAG line
    carries a pager message. A line whose address or function will not parse is not a page.
    """
    m = _LINE.match((line or "").rstrip("\n"))
    if not m:
        return None
    try:
        address = _maybe_int(m["addr"])
        function = _maybe_int(m["func"])
    except ValueError:
        return None
    label = m["label"]
    if label is None:
        kind, text = "tone", None
    else:
        kind = label.lower()
        text = (m["text"] or "").rstrip() or None
    return PocsagPage(baud=int(m["baud"]), address=address, function=function,
                      kind=kind, text=text)


class PageLog:
    """A bounded, newest-first history of decoded pager messages."""

    def __init__(self, maxlen: int = DEFAULT_LOG_LEN):
        self._pages = deque(maxlen=maxlen)

    def add(self, page: PocsagPage | None) -> None:
        if page is not None:
            self._pages.append(page)

    def recent(self, limit: int | None = None) -> list:
        """The most recent pages, newest first, optionally capped at `limit`."""
        pages = list(reversed(self._pages))
        return pages[:limit] if limit else pages


class _Pipeline:
    """The rtl_fm|multimon-ng pair as one object: multimon-ng's stdout to read, both to reap."""

    def __init__(self, rtl_fm, multimon):
        self._rtl_fm = rtl_fm
        self._multimon = multimon
        self.stdout = multimon.stdout

    def poll(self):
        return self._multimon.poll()

    def terminate(self):
        for proc in (self._multimon, self._rtl_fm):
            try:
                proc.terminate()
            except (OSError, subprocess.SubprocessError):
                pass

    def wait(self, timeout=None):
        for proc in (self._rtl_fm, self._multimon):
            try:
                proc.wait(timeout=timeout)
            except (OSError, subprocess.SubprocessError):
                pass
        return 0


def _popen(freq_hz: int, device: int):
    """Launch rtl_fm demodulating the pager channel into multimon-ng, which prints POCSAG pages."""
    rtl_fm = subprocess.Popen(
        [RTL_FM, "-M", "fm", "-f", str(freq_hz), "-s", str(POCSAG_SAMPLE_RATE),
         "-g", "49.6", "-l", "0", "-d", str(device)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    demods = []
    for name in POCSAG_DEMODS:
        demods += ["-a", name]
    multimon = subprocess.Popen(
        [MULTIMON, "-t", "raw", *demods, "-"],
        stdin=rtl_fm.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    # Let rtl_fm see SIGPIPE if multimon-ng dies, rather than fill a pipe nobody drains.
    if rtl_fm.stdout is not None:
        rtl_fm.stdout.close()
    return _Pipeline(rtl_fm, multimon)


class PocsagDecoder:
    """Owns one rtl_fm|multimon-ng pipeline and the log of pages it decodes.

    A sibling of the APRS decoder: an SDR-exclusive tool with a reader thread off the request path
    and release/resume so it takes turns with the waterfall on the one dongle.
    """

    def __init__(self, *, freq_hz=DEFAULT_POCSAG_HZ, device=0, spawn=_popen,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None):
        self.freq_hz = int(freq_hz)
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._log = PageLog()
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
            raise PocsagUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed."""
        with self._life:
            self._released = True
            self.stop()
            with self._lock:
                self._log = PageLog()

    def resume(self) -> None:
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin decoding POCSAG, unless the radio is absent, a tool is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        if self._released:
            raise PocsagUnavailable(
                "the radio was released to another program; resume POCSAG to take it back")
        for tool in (RTL_FM, MULTIMON):
            if not self._which(tool):
                raise PocsagUnavailable(
                    f"{tool} is not installed, so SIGINT 101 cannot decode POCSAG")
        if not self._radio_present():
            raise PocsagUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a page")
        if self.error:
            reason, self.error = self.error, None
            raise PocsagUnavailable(reason)
        self.device = self._choose_device()
        try:
            self._proc = self._spawn(self.freq_hz, self.device)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise PocsagUnavailable(f"cannot run the POCSAG pipeline: {exc}") from exc
        self.error = None
        self._thread = threading.Thread(target=self._read, name="sigint101-pocsag", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Consume multimon-ng's output on a background thread, logging pages until it ends."""
        try:
            for raw in self._proc.stdout:
                page = parse_pocsag_line(raw)
                if page is not None:
                    with self._lock:
                        self._log.add(page)
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
                self.error = "the POCSAG pipeline did not stop; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. A real POCSAG stream never ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def pages(self, limit: int | None = None) -> list:
        """The recent pages as plain dicts for the API, newest first."""
        with self._lock:
            rows = self._log.recent(limit)
        return [p.as_dict() for p in rows]
