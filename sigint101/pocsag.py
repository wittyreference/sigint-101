# ABOUTME: Decodes POCSAG pager framing via rtl_fm|multimon-ng for M15; on the commercial band the
# ABOUTME: words are dropped at parse time. No radio, no pages: an empty log, never an invented one.

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass

from .childproc import ChildWatch, running_command, spawn, stop_process
from .childproc import Pipeline as _Pipeline
from .hardware import (
    MULTIMON,
    RTL_FM,
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# Where the POCSAG lesson listens: an assigned 929 MHz paging channel. 47 CFR 90.493(a) puts those
# channels at 929.0125 MHz plus n x 25 kHz, so 929.6125 is one (929.6000 sat between two).
DEFAULT_POCSAG_HZ = 929_612_500

# The US commercial paging band (Part 22 and Part 90 paging, 929-932 MHz). Pages here are carried
# by a paging company, so under 18 USC 2510(16)(D) they are not "readily accessible to the general
# public": decoding the words is interception (2511(1)(a)); only tone-only pages are excluded from
# "electronic communication" (2510(12)(B)). On this band the console shows the framing only.
COMMERCIAL_PAGING_LOW_HZ = 929_000_000
COMMERCIAL_PAGING_HIGH_HZ = 932_000_000

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


def on_commercial_paging_band(freq_hz: int) -> bool:
    """Is this frequency in the US commercial paging band, where a page's words are not ours?"""
    return COMMERCIAL_PAGING_LOW_HZ <= int(freq_hz) <= COMMERCIAL_PAGING_HIGH_HZ


class PocsagUnavailable(RuntimeError):
    """The POCSAG stream cannot be read. Never a reason to invent a page — the log stays empty."""


@dataclass(frozen=True)
class PocsagPage:
    """One decoded pager message: who it was addressed to, how it was framed, and — only where the
    words are lawfully ours — what it carried. A withheld page keeps its length, never its text."""

    baud: int
    address: int | None
    function: int | None
    kind: str                  # alpha | numeric | skyper | tone
    text: str | None = None
    length: int = 0            # characters (or digits) in the message; 0 for a tone-only page
    text_withheld: bool = False

    def as_dict(self) -> dict:
        return {
            "baud": self.baud,
            "address": self.address,
            "function": self.function,
            "kind": self.kind,
            "text": self.text,
            "length": self.length,
            "text_withheld": self.text_withheld,
        }


def _maybe_int(token: str):
    return None if token == "-" else int(token)


def parse_pocsag_line(line: str, *, keep_text: bool = False) -> PocsagPage | None:
    """Turn one multimon-ng POCSAG line into a PocsagPage, or None if it is not one.

    multimon-ng's other demodulators and its status output take other shapes; only a POCSAG line
    carries a pager message. A line whose address or function will not parse is not a page.

    The words are withheld unless the caller says they may be kept (`keep_text`): the page keeps
    its framing and its length, and the text is dropped here, before anything can store, log or
    serve it. Withholding is the default so no caller can forget to mask.
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
    length = len(text) if text else 0
    withheld = text is not None and not keep_text
    return PocsagPage(baud=int(m["baud"]), address=address, function=function,
                      kind=kind, text=None if withheld else text, length=length,
                      text_withheld=withheld)


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


def _popen(freq_hz: int, device: int):
    """Launch rtl_fm demodulating the pager channel into multimon-ng, which prints POCSAG pages."""
    rtl_fm = spawn(
        [RTL_FM, "-M", "fm", "-f", str(freq_hz), "-s", str(POCSAG_SAMPLE_RATE),
         "-g", "49.6", "-l", "0", "-d", str(device)],
        stdout=subprocess.PIPE)
    demods = []
    for name in POCSAG_DEMODS:
        demods += ["-a", name]
    multimon = spawn(
        [MULTIMON, "-t", "raw", *demods, "-"],
        stdin=rtl_fm.stdout, stdout=subprocess.PIPE, text=True)
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
        # The frequency this decoder was built for; the console tunes a lesson back to it.
        self.home_hz = self.freq_hz
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._log = PageLog()
        # Called with each page as a plain dict (its words already withheld on the commercial
        # band), from the reader thread only and outside the log lock. The console's hook logs it
        # as a reception and never raises, so the call is made directly: it cannot end the decode.
        self.on_reception = None
        self._lock = threading.Lock()
        self._life = threading.RLock()
        self._proc = None
        self._thread = None
        self._released = False
        self.error = None
        # Who ended the child, and when the console may launch it again after it died on its own.
        self.watch = ChildWatch(RTL_FM)

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def live_command(self) -> str | None:
        """The exact command running now, read off the live process; None when nothing runs."""
        return running_command(self._proc)

    @property
    def released(self) -> bool:
        return self._released

    @property
    def keeps_text(self) -> bool:
        """May this decoder keep a page's words? Never on the commercial paging band."""
        return not on_commercial_paging_band(self.freq_hz)

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

    def retune(self, freq_hz) -> None:
        """Move to another frequency: the running pipeline stops, and the next start() spawns there.

        The pages heard on the old frequency are cleared as release() clears them, but the dongle
        is not handed away. Tuning to where the decoder already is changes nothing. Whether the
        next child may keep a page's words is decided by the band it is spawned on.
        """
        with self._life:
            if int(freq_hz) == self.freq_hz:
                return
            self._stop_locked()
            self.freq_hz = int(freq_hz)
            with self._lock:
                self._log = PageLog()

    def start(self) -> None:
        """Begin decoding POCSAG, unless the radio is absent, a tool is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        self._settle()
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
        # A child that died on its own is not relaunched on every poll: the reason stands, and the
        # console waits out a back-off that grows while the deaths keep coming.
        blocked = self.watch.blocked()
        if blocked:
            raise PocsagUnavailable(blocked)
        self.device = self._choose_device()
        spawned_hz = self.freq_hz
        try:
            self._proc = self._spawn(spawned_hz, self.device)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise PocsagUnavailable(f"cannot run the POCSAG pipeline: {exc}") from exc
        self.watch.spawned(self._proc)
        self.error = None
        # The band rule is fixed by the frequency this child was spawned on, never read live: a
        # retune can then never let a line heard on one band be parsed under another band's rule.
        keep_text = not on_commercial_paging_band(spawned_hz)
        self._thread = threading.Thread(target=self._read, args=(self._proc, keep_text),
                                        name="sigint101-pocsag", daemon=True)
        self._thread.start()

    def _read(self, proc, keep_text: bool = False) -> None:
        """Consume multimon-ng's output on a background thread, logging pages until it ends.

        `keep_text` is the band rule of the frequency `proc` was spawned on; withholding is the
        default, as in parse_pocsag_line, so no caller can forget to mask.
        """
        try:
            for raw in proc.stdout:
                page = parse_pocsag_line(raw, keep_text=keep_text)
                if page is not None:
                    with self._lock:
                        self._log.add(page)
                    report = self.on_reception
                    if report is not None:
                        report(page.as_dict())
        except (OSError, ValueError) as exc:  # loud, never silent
            self.error = str(exc)
        finally:
            self._note_end(proc)

    def _note_end(self, proc) -> None:
        """Record why the child ended, if it was not us, so the next poll reports it as stopped."""
        reason = self.watch.ended(proc)
        if reason and self.error is None:
            self.error = reason

    def _settle(self) -> None:
        """Account for a child that ended since the last poll, then clear away what it left."""
        if self._proc is not None:
            self._note_end(self._proc)
            self._stop_locked()

    def stop(self) -> None:
        """End decoding and let go of the dongle."""
        with self._life:
            self._stop_locked()

    def _stop_locked(self) -> None:
        # Marked first, so the reader seeing the output end never reports our stop as a death.
        self.watch.stopping()
        proc, self._proc = self._proc, None
        if proc is not None and not stop_process(proc):
            self.error = "the POCSAG pipeline survived kill; the dongle may still be held"
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
