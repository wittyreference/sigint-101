# ABOUTME: Decodes APRS off the 2m packet channel via rtl_fm|multimon-ng for M14 — a framed packet.
# ABOUTME: No radio, no multimon-ng, no packets — reports an empty roster, never invents a station.

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

from .hardware import (
    MULTIMON,
    RTL_FM,
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# Where the APRS lesson listens. 144.390 MHz is the North American 2m APRS channel; a pack can
# point it elsewhere via tune, but this is the one almost all traffic rides.
DEFAULT_APRS_HZ = 144_390_000

# multimon-ng expects 16-bit signed mono at 22050 Hz on its raw input; rtl_fm is told to match.
APRS_SAMPLE_RATE = 22050

# The prefix multimon-ng prints before each decoded APRS frame (TNC2 monitor format).
_APRS_PREFIX = "APRS: "

# An uncompressed APRS position: DDMM.hhN / DDDMM.hhW, with a symbol-table char between the two and
# a symbol code after the longitude. The data-type char (!=@/) and any timestamp are stripped first.
_POSITION = re.compile(
    r"^(?P<lat>\d{4}\.\d{2})(?P<ns>[NS]).(?P<lon>\d{5}\.\d{2})(?P<ew>[EW]).(?P<comment>.*)$")


class AprsUnavailable(RuntimeError):
    """The APRS stream cannot be read. Never a reason to invent a station — roster stays empty."""


@dataclass(frozen=True)
class AprsPacket:
    """One decoded APRS frame: its AX.25 addressing and the meaning of its information field."""

    source: str
    dest: str
    path: list
    info: str
    kind: str = "other"           # position | message | status | other
    lat: float | None = None
    lon: float | None = None
    text: str | None = None       # the position comment, status text, or message body
    addressee: str | None = None  # for a message, who it is addressed to


def parse_aprs_line(line: str) -> str | None:
    """Pull the raw TNC2 frame out of one multimon-ng line, or None if it is not an APRS frame.

    multimon-ng prints each decoded APRS frame as "APRS: <tnc2>"; its other demodulators and status
    messages take other shapes, so a line without the APRS prefix is not a frame to parse.
    """
    text = (line or "").rstrip("\n")
    if not text.startswith(_APRS_PREFIX):
        return None
    frame = text[len(_APRS_PREFIX):].strip()
    return frame or None


def _parse_latlon(body: str):
    """Decimal (lat, lon) and the trailing comment from an uncompressed position, or three Nones."""
    m = _POSITION.match(body)
    if not m:
        return None, None, None
    lat = int(m["lat"][:2]) + float(m["lat"][2:]) / 60.0
    if m["ns"] == "S":
        lat = -lat
    lon = int(m["lon"][:3]) + float(m["lon"][3:]) / 60.0
    if m["ew"] == "W":
        lon = -lon
    return lat, lon, m["comment"].strip() or None


def _classify(info: str):
    """Turn an APRS information field into (kind, lat, lon, text, addressee)."""
    if not info:
        return "other", None, None, None, None
    dti = info[0]
    if dti in "!=":
        lat, lon, comment = _parse_latlon(info[1:])
        return ("position" if lat is not None else "other"), lat, lon, comment, None
    if dti in "@/":
        # A timestamped position: the 7-char time group precedes the latitude.
        lat, lon, comment = _parse_latlon(info[8:])
        return ("position" if lat is not None else "other"), lat, lon, comment, None
    if dti == ">":
        return "status", None, None, info[1:].strip() or None, None
    if dti == ":":
        # Message: ":ADDRESSEE :text{seq" — a 9-char addressee field, then the body.
        addressee = info[1:10].strip()
        body = info[10:]
        if body.startswith(":"):
            body = body[1:]
        body = body.split("{", 1)[0].strip()
        return "message", None, None, body or None, addressee or None
    return "other", None, None, None, None


def parse_aprs_frame(frame: str | None) -> AprsPacket | None:
    """Turn one raw TNC2 frame (SRC>DST,DIGI,...:info) into an AprsPacket, or None if malformed.

    The header is everything before the first colon; the information field is the rest (a message's
    own colons live inside the info, so only the first colon separates header from info).
    """
    if not frame or ":" not in frame:
        return None
    header, info = frame.split(":", 1)
    if ">" not in header:
        return None
    source, rest = header.split(">", 1)
    parts = rest.split(",")
    dest = parts[0]
    path = parts[1:]
    if not source or not dest:
        return None
    kind, lat, lon, text, addressee = _classify(info)
    return AprsPacket(source=source, dest=dest, path=path, info=info,
                      kind=kind, lat=lat, lon=lon, text=text, addressee=addressee)


@dataclass
class Station:
    """One station as last heard: its latest packet, and how many we have decoded from it."""

    source: str
    packet: AprsPacket
    packet_count: int = 0
    last_seen: float = 0.0

    @property
    def text(self):
        return self.packet.text


class StationRoster:
    """Every station heard on the channel, keyed by callsign, with its most recent packet.

    APRS stations beacon their position, status and messages every minute or few; this keeps the
    latest packet from each so the panel reads like a monitor's station list — who is on the air and
    what they last said — not a scrolling log.
    """

    def __init__(self):
        self._by_source: dict[str, Station] = {}

    def absorb(self, packet: AprsPacket | None, now: float) -> None:
        if packet is None:
            return
        station = self._by_source.get(packet.source)
        if station is None:
            station = Station(source=packet.source, packet=packet)
            self._by_source[packet.source] = station
        station.packet = packet
        station.packet_count += 1
        station.last_seen = now

    def stations(self, now: float) -> list:
        """Every station heard, freshest first."""
        return sorted(self._by_source.values(), key=lambda s: s.last_seen, reverse=True)


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
    """Launch rtl_fm demodulating the 2m channel into multimon-ng, which prints APRS frames."""
    rtl_fm = subprocess.Popen(
        [RTL_FM, "-M", "fm", "-f", str(freq_hz), "-s", str(APRS_SAMPLE_RATE),
         "-g", "49.6", "-l", "0", "-d", str(device)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    multimon = subprocess.Popen(
        [MULTIMON, "-t", "raw", "-a", "AFSK1200", "-A", "-"],
        stdin=rtl_fm.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    # Let rtl_fm see SIGPIPE if multimon-ng dies, rather than fill a pipe nobody drains.
    if rtl_fm.stdout is not None:
        rtl_fm.stdout.close()
    return _Pipeline(rtl_fm, multimon)


class AprsDecoder:
    """Owns one rtl_fm|multimon-ng pipeline and the station roster it assembles.

    A sibling of the RDS decoder: an SDR-exclusive tool with a reader thread off the request path
    and release/resume so it takes turns with the waterfall on the one dongle.
    """

    def __init__(self, *, freq_hz=DEFAULT_APRS_HZ, device=0, spawn=_popen,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None):
        self.freq_hz = int(freq_hz)
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._roster = StationRoster()
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
            raise AprsUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed."""
        with self._life:
            self._released = True
            self.stop()
            with self._lock:
                self._roster = StationRoster()

    def resume(self) -> None:
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin decoding APRS, unless the radio is absent, a tool is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        if self._released:
            raise AprsUnavailable(
                "the radio was released to another program; resume APRS to take it back")
        for tool in (RTL_FM, MULTIMON):
            if not self._which(tool):
                raise AprsUnavailable(f"{tool} is not installed, so SIGINT 101 cannot decode APRS")
        if not self._radio_present():
            raise AprsUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a station")
        if self.error:
            reason, self.error = self.error, None
            raise AprsUnavailable(reason)
        self.device = self._choose_device()
        try:
            self._proc = self._spawn(self.freq_hz, self.device)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise AprsUnavailable(f"cannot run the APRS pipeline: {exc}") from exc
        self.error = None
        self._thread = threading.Thread(target=self._read, name="sigint101-aprs", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Consume multimon-ng's output on a background thread, building the roster till it ends."""
        try:
            for raw in self._proc.stdout:
                packet = parse_aprs_frame(parse_aprs_line(raw))
                if packet is not None:
                    with self._lock:
                        self._roster.absorb(packet, now=time.time())
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
                self.error = "the APRS pipeline did not stop; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. A real APRS stream never ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def stations(self) -> list:
        """The roster so far, as plain dicts for the API, freshest station first."""
        now = time.time()
        with self._lock:
            rows = self._roster.stations(now=now)
        out = []
        for s in rows:
            p = s.packet
            out.append({
                "source": s.source,
                "dest": p.dest,
                "path": list(p.path),
                "kind": p.kind,
                "lat": p.lat,
                "lon": p.lon,
                "text": p.text,
                "addressee": p.addressee,
                "packet_count": s.packet_count,
            })
        return out
