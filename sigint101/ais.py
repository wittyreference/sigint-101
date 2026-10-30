# ABOUTME: Decodes AIS ship reports via rtl_ais for M26 — the AIVDM sentences vessels broadcast.
# ABOUTME: No radio, no rtl_ais, no ships — reports an empty roster, never invents a vessel.

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass

from .hardware import (
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# rtl_ais (github.com/dgiardini/rtl-ais) opens the dongle, demodulates BOTH AIS channels at once,
# and sends decoded AIVDM/AIVDO NMEA sentences over UDP. These are the two channels it listens on,
# shown to the learner rather than tuned — rtl_ais is fixed to the marine AIS pair.
AIS_CHANNEL_A_HZ = 161_975_000
AIS_CHANNEL_B_HZ = 162_025_000

# Where rtl_ais sends its NMEA sentences by default (-h host / -P port). The decoder binds this
# UDP socket itself and reads the datagrams rtl_ais pushes to it, so it never holds the dongle —
# rtl_ais does, exactly as dump1090 does for ADS-B.
DEFAULT_AIS_HOST = "127.0.0.1"
DEFAULT_AIS_PORT = 10110

# rtl_ais's binary. Defined here, not in hardware.py, so parallel decoder builds never collide.
RTL_AIS = "rtl_ais"

# ITU-R M.1371 navigation-status enumeration (field at bits 38-41 of a type 1/2/3 report). The
# code is stored as its text so the panel reads it the way a bridge officer would; 15 is the
# "not defined" default a transponder sends when the status is unset.
NAV_STATUS = (
    "Under way using engine", "At anchor", "Not under command",
    "Restricted manoeuverability", "Constrained by her draught", "Moored",
    "Aground", "Engaged in fishing", "Under way sailing",
    "Reserved (HSC)", "Reserved (WIG)", "Reserved", "Reserved",
    "Reserved", "AIS-SART", "Not defined",
)

# The AIS six-bit character set: values 0-31 map to '@'..'_', values 32-63 to ' '..'?'. Used for
# vessel names and call signs, which are padded on the right with '@' (and spaces).
_SIXBIT = "".join(chr(v + 64) if v < 32 else chr(v) for v in range(64))


class AisUnavailable(RuntimeError):
    """The AIS feed cannot be read. Never a reason to invent a ship — the roster stays empty."""


def _unarmor(payload: str, fill: int = 0) -> str:
    """Turn an armored AIVDM payload into its raw bit string (MSB first).

    Each character carries six bits: subtract 48 from its ASCII value, subtract 8 more if the
    result exceeds 40. The trailing `fill` bits are padding to the next 6-bit boundary and are
    dropped, which only matters for a message whose real length is not a multiple of six.
    """
    bits = []
    for ch in payload:
        value = ord(ch) - 48
        if value > 40:
            value -= 8
        bits.append(format(value & 0x3F, "06b"))
    joined = "".join(bits)
    if fill:
        joined = joined[: len(joined) - fill]
    return joined


def _u(bits: str, start: int, length: int):
    """Unsigned integer from a bit-field, or None if the payload is too short to hold it."""
    chunk = bits[start:start + length]
    if len(chunk) < length:
        return None
    return int(chunk, 2)


def _i(bits: str, start: int, length: int):
    """Two's-complement signed integer from a bit-field, or None if the payload is too short."""
    value = _u(bits, start, length)
    if value is None:
        return None
    if value >= (1 << (length - 1)):
        value -= (1 << length)
    return value


def _text(bits: str, start: int, length: int):
    """Decode a six-bit text field, stripping the '@'/space padding, or None if it is empty."""
    out = []
    end = min(start + length, len(bits))
    pos = start
    while pos + 6 <= end:
        out.append(_SIXBIT[int(bits[pos:pos + 6], 2)])
        pos += 6
    return "".join(out).rstrip("@ ") or None


@dataclass(frozen=True)
class AisSentence:
    """One parsed AIVDM line: the NMEA framing around a (possibly partial) armored payload."""

    frag_count: int
    frag_num: int
    seq_id: str | None
    channel: str
    payload: str
    fill: int


def _checksum(body: str) -> str:
    """The NMEA checksum of `body` (everything between '!' and '*'): XOR of the bytes, as hex."""
    value = 0
    for ch in body:
        value ^= ord(ch)
    return f"{value:02X}"


def parse_ais_sentence(line: str) -> AisSentence | None:
    """Parse one AIVDM/AIVDO NMEA line into its fields, or None if it is not a sound sentence.

    A sound sentence starts with '!', carries a '*' checksum that matches its body, has the seven
    comma-separated fields of an !AIVDM sentence, and a non-empty payload. Anything else — a line
    from another talker, a truncated sentence, a corrupted one — is not an AIS sentence to read.
    """
    text = (line or "").strip()
    if not text.startswith("!") or "*" not in text:
        return None
    body, _, checksum = text[1:].partition("*")
    if _checksum(body) != checksum[:2].upper():
        return None
    fields = body.split(",")
    if len(fields) != 7 or fields[0] not in ("AIVDM", "AIVDO"):
        return None
    payload = fields[5]
    if not payload:
        return None
    try:
        frag_count = int(fields[1])
        frag_num = int(fields[2])
        fill = int(fields[6])
    except ValueError:
        return None
    return AisSentence(frag_count, frag_num, fields[3] or None, fields[4], payload, fill)


@dataclass(frozen=True)
class AisReport:
    """One decoded AIS message.

    A position report (type 1/2/3) fills the kinematic fields; a static-and-voyage report (type 5)
    fills name/call sign/ship type. The roster is what merges the two halves of a vessel's picture.
    """

    msg_type: int
    mmsi: int
    lat: float | None = None
    lon: float | None = None
    sog_kt: float | None = None
    cog_deg: float | None = None
    heading_deg: int | None = None
    nav_status: str | None = None
    name: str | None = None
    call_sign: str | None = None
    ship_type: int | None = None

    def as_dict(self) -> dict:
        return {
            "msg_type": self.msg_type,
            "mmsi": self.mmsi,
            "lat": self.lat,
            "lon": self.lon,
            "sog_kt": self.sog_kt,
            "cog_deg": self.cog_deg,
            "heading_deg": self.heading_deg,
            "nav_status": self.nav_status,
            "name": self.name,
            "call_sign": self.call_sign,
            "ship_type": self.ship_type,
        }


def _coord(raw, limit: float):
    """Scale a raw lat/lon field to degrees, or None for the out-of-range not-available sentinel."""
    if raw is None:
        return None
    degrees = raw / 600000.0
    return degrees if abs(degrees) <= limit else None


def _decode_position(msg_type: int, mmsi: int, bits: str) -> AisReport:
    """Decode a type 1/2/3 Class A position report from its 168-bit payload."""
    status = _u(bits, 38, 4)
    nav_status = NAV_STATUS[status] if status is not None and status < len(NAV_STATUS) else None
    sog = _u(bits, 50, 10)
    cog = _u(bits, 116, 12)
    heading = _u(bits, 128, 9)
    return AisReport(
        msg_type=msg_type,
        mmsi=mmsi,
        lat=_coord(_i(bits, 89, 27), 90.0),
        lon=_coord(_i(bits, 61, 28), 180.0),
        sog_kt=None if sog is None or sog == 1023 else sog / 10.0,
        cog_deg=None if cog is None or cog == 3600 else cog / 10.0,
        heading_deg=None if heading is None or heading == 511 else heading,
        nav_status=nav_status,
    )


def _decode_static(mmsi: int, bits: str) -> AisReport:
    """Decode a type 5 static-and-voyage report (name, call sign, ship type) from its payload."""
    return AisReport(
        msg_type=5,
        mmsi=mmsi,
        call_sign=_text(bits, 70, 42),
        name=_text(bits, 112, 120),
        ship_type=_u(bits, 232, 8),
    )


def decode_payload(payload: str, fill: int = 0) -> AisReport | None:
    """Decode a complete (reassembled) AIVDM payload into an AisReport, or None if not a ship.

    Only the messages this lesson rosters are decoded: types 1/2/3 (Class A position) and type 5
    (static and voyage data). Other message types — base stations, aids to navigation — and any
    payload too short to hold an MMSI return None, so the roster is never fed a guess.
    """
    bits = _unarmor(payload, fill)
    msg_type = _u(bits, 0, 6)
    mmsi = _u(bits, 8, 30)
    if msg_type is None or mmsi is None:
        return None
    if msg_type in (1, 2, 3):
        return _decode_position(msg_type, mmsi, bits)
    if msg_type == 5:
        return _decode_static(mmsi, bits)
    return None


class AisAssembler:
    """Turns a stream of AIVDM sentences into decoded reports, reassembling multi-part messages.

    A single-sentence message decodes at once; a multi-part one (a type 5 spans two sentences) is
    buffered by its sequence id and channel until every fragment has arrived, then the armored
    payloads are concatenated and decoded as one. Push a line, get a report or None.
    """

    def __init__(self):
        self._parts: dict = {}

    def push(self, line: str) -> AisReport | None:
        sentence = parse_ais_sentence(line)
        if sentence is None:
            return None
        if sentence.frag_count <= 1:
            return decode_payload(sentence.payload, sentence.fill)
        key = (sentence.seq_id, sentence.channel)
        buffer = self._parts.setdefault(key, {"parts": {}, "fill": 0})
        buffer["parts"][sentence.frag_num] = sentence.payload
        if sentence.frag_num == sentence.frag_count:
            buffer["fill"] = sentence.fill          # the final fragment carries the pad count
        parts = buffer["parts"]
        if all(n in parts for n in range(1, sentence.frag_count + 1)):
            payload = "".join(parts[n] for n in range(1, sentence.frag_count + 1))
            del self._parts[key]
            return decode_payload(payload, buffer["fill"])
        return None


@dataclass
class Vessel:
    """One vessel assembled from the stream of reports that share its MMSI."""

    mmsi: int
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    sog_kt: float | None = None
    cog_deg: float | None = None
    heading_deg: int | None = None
    nav_status: str | None = None
    ship_type: int | None = None
    message_count: int = 0
    last_seen: float = 0.0

    # The fields a later report may carry; each is kept from the last report that set it, so a
    # type-5 name survives the type-1 positions that follow it and vice versa.
    _MERGE = ("name", "lat", "lon", "sog_kt", "cog_deg", "heading_deg", "nav_status", "ship_type")

    def absorb(self, report: AisReport, now: float) -> None:
        self.message_count += 1
        self.last_seen = now
        for field in self._MERGE:
            value = getattr(report, field)
            if value is not None:
                setattr(self, field, value)

    def as_dict(self) -> dict:
        return {
            "mmsi": self.mmsi,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "sog_kt": self.sog_kt,
            "cog_deg": self.cog_deg,
            "heading_deg": self.heading_deg,
            "nav_status": self.nav_status,
            "ship_type": self.ship_type,
            "message_count": self.message_count,
            "last_seen": self.last_seen,
        }


class VesselRoster:
    """Every vessel heard on the AIS channels, keyed by MMSI, freshest contact first.

    Like the APRS station roster this keeps the latest picture of each sender rather than a
    scrolling log; a vessel beacons its position every few seconds and its name every few minutes,
    and the roster merges those into one entry so the panel reads like a bridge's traffic list.
    """

    def __init__(self):
        self._by_mmsi: dict[int, Vessel] = {}

    def absorb(self, report: AisReport | None, now: float) -> None:
        if report is None:
            return
        vessel = self._by_mmsi.get(report.mmsi)
        if vessel is None:
            vessel = Vessel(mmsi=report.mmsi)
            self._by_mmsi[report.mmsi] = vessel
        vessel.absorb(report, now)

    def vessels(self, limit: int | None = None) -> list:
        """Every vessel heard, freshest first, optionally capped at `limit`."""
        rows = sorted(self._by_mmsi.values(), key=lambda v: v.last_seen, reverse=True)
        return rows[:limit] if limit else rows


class _Feed:
    """The live rtl_ais process and its UDP socket as one object: lines to read, both to tear down.

    rtl_ais holds the dongle and pushes NMEA sentences over UDP; this binds the socket, so the
    reader consumes datagrams exactly as the other decoders consume a subprocess's stdout.
    """

    def __init__(self, proc: subprocess.Popen, sock: socket.socket):
        self._proc = proc
        self._sock = sock
        self.stdout = self._lines()

    def _lines(self):
        with self._sock:
            while True:
                try:
                    data = self._sock.recv(4096)
                except OSError:
                    return
                for line in data.decode("ascii", "replace").splitlines():
                    line = line.strip()
                    if line:
                        yield line

    def poll(self):
        return self._proc.poll()

    def terminate(self):
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            self._proc.terminate()
        except (OSError, subprocess.SubprocessError):
            pass

    def wait(self, timeout=None):
        try:
            return self._proc.wait(timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return 0


def _popen(device: int, host: str = DEFAULT_AIS_HOST, port: int = DEFAULT_AIS_PORT) -> _Feed:
    """Bind the AIS UDP socket, launch rtl_ais pointed at it, and return the two as one feed."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise AisUnavailable(f"cannot bind {host}:{port} to read rtl_ais — {exc}") from exc
    try:
        proc = subprocess.Popen(
            [RTL_AIS, "-d", str(device), "-h", host, "-P", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, ValueError) as exc:
        sock.close()
        raise AisUnavailable(f"cannot run rtl_ais: {exc}") from exc
    return _Feed(proc, sock)


class AisDecoder:
    """Owns one rtl_ais feed and the vessel roster it assembles.

    A sibling of the ADS-B decoder: an SDR-exclusive tool with a reader thread off the request path
    and release/resume so it takes turns with the waterfall on the one dongle. rtl_ais opens the
    radio itself, so there is no rtl_fm front end — the decoder only reads the UDP sentences.
    """

    def __init__(self, *, device=0, spawn=_popen, radio_present=None, which=shutil.which,
                 host=DEFAULT_AIS_HOST, port=DEFAULT_AIS_PORT, serial=None, list_devices=None):
        self.device = device
        self.host = host
        self.port = port
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._roster = VesselRoster()
        self._assembler = AisAssembler()
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
            raise AisUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed."""
        with self._life:
            self._released = True
            self.stop()
            with self._lock:
                self._roster = VesselRoster()
                self._assembler = AisAssembler()

    def resume(self) -> None:
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin decoding AIS, unless the radio is absent, rtl_ais is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        if self._released:
            raise AisUnavailable(
                "the radio was released to another program; resume AIS to take it back")
        if not self._which(RTL_AIS):
            raise AisUnavailable(
                f"{RTL_AIS} is not installed, so SIGINT 101 cannot decode AIS")
        if not self._radio_present():
            raise AisUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a vessel")
        if self.error:
            reason, self.error = self.error, None
            raise AisUnavailable(reason)
        self.device = self._choose_device()
        self._assembler = AisAssembler()
        try:
            self._proc = self._spawn(self.device, self.host, self.port)
        except AisUnavailable:
            self._proc = None
            raise
        except (OSError, ValueError) as exc:
            self._proc = None
            raise AisUnavailable(f"cannot run rtl_ais: {exc}") from exc
        self.error = None
        self._thread = threading.Thread(target=self._read, name="sigint101-ais", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Consume rtl_ais's NMEA sentences on a thread, building the roster until it ends."""
        try:
            for raw in self._proc.stdout:
                report = self._assembler.push(raw)
                if report is not None:
                    with self._lock:
                        self._roster.absorb(report, now=time.time())
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
                self.error = "rtl_ais did not stop; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. A real AIS feed never ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def vessels(self, limit: int | None = None) -> list:
        """The roster so far, as plain dicts for the API, freshest vessel first."""
        with self._lock:
            rows = self._roster.vessels(limit)
        return [v.as_dict() for v in rows]
