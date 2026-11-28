# ABOUTME: Decodes AIS ship reports via rtl_ais for M26 — the AIVDM sentences vessels broadcast.
# ABOUTME: No radio, no rtl_ais, no ships — reports an empty roster, never invents a vessel.

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass

from .childproc import (
    ChildWatch,
    RunGuard,
    command_line,
    spawn,
    stop_process,
)
from .hardware import (
    DeviceSelectionError,
    probe_rtlsdr,
    rtlsdr_radio_for_run,
)
from .receiver_lifecycle import PartialLaunch, SupervisedReceiver
from .receiver_socket import PortOccupied, bind_receiver

# rtl_ais (github.com/dgiardini/rtl-ais) opens the dongle, demodulates BOTH AIS channels at once,
# and sends decoded AIVDM/AIVDO NMEA sentences over UDP. These are the two channels it listens on,
# shown to the learner rather than tuned — rtl_ais is fixed to the marine AIS pair.
AIS_CHANNEL_A_HZ = 161_975_000
AIS_CHANNEL_B_HZ = 162_025_000

# The decoder binds the stock rtl_ais destination, then reads NMEA from the companion's
# inherited UDP socket. The companion opens the dongle, as dump1090 does for ADS-B.
DEFAULT_AIS_HOST = "127.0.0.1"
DEFAULT_AIS_PORT = 10110

# How often the feed's reader wakes from a quiet socket to check whether it was stopped or
# rtl_ais has died. UDP has no end-of-stream, so without this a reader waits forever for both.
FEED_POLL_S = 0.5

# How long the first part of a multi-part message waits for the rest. The parts of one message
# leave rtl_ais back to back; a part older than this belongs to a message that will never finish.
FRAGMENT_MAX_AGE_S = 2.0

RTL_AIS = "rtl_ais"
AIS_COMPANION = "rtl_ais-sigint101"
AIS_MODE = "--sigint101-udp-stdout"
AIS_CAPABILITY_QUERY = "--sigint101-capability"
AIS_CAPABILITY = b"sigint101-ais-udp-v1\n"
AIS_CAPABILITY_TIMEOUT_S = 1.0

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


class AisPortOccupied(AisUnavailable, PortOccupied):
    """The local AIS UDP port cannot be bound."""


class AisFeedError(OSError):
    """The running companion's UDP feed could not be read or verified."""


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

    def __init__(self, *, clock=time.monotonic, max_age_s=FRAGMENT_MAX_AGE_S):
        self._parts: dict = {}
        self._clock = clock
        self._max_age_s = max_age_s

    def push(self, line: str) -> AisReport | None:
        sentence = parse_ais_sentence(line)
        if sentence is None:
            return None
        if sentence.frag_count <= 1:
            return decode_payload(sentence.payload, sentence.fill)
        now = self._clock()
        # A stale half message must never complete a newer one: that decodes as a real-looking
        # vessel with a garbled name or callsign. Old parts expire, and a first part always
        # begins afresh, because rtl_ais reuses sequence ids.
        self._parts = {k: b for k, b in self._parts.items()
                       if now - b["at"] <= self._max_age_s}
        key = (sentence.seq_id, sentence.channel)
        if sentence.frag_num == 1:
            self._parts.pop(key, None)
        buffer = self._parts.setdefault(key, {"parts": {}, "fill": 0, "at": now})
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

    def absorb(self, report: AisReport | None, now: float) -> Vessel | None:
        """Fold one report into its vessel's entry, and return that vessel as it now stands."""
        if report is None:
            return None
        vessel = self._by_mmsi.get(report.mmsi)
        if vessel is None:
            vessel = Vessel(mmsi=report.mmsi)
            self._by_mmsi[report.mmsi] = vessel
        vessel.absorb(report, now)
        return vessel

    def vessels(self, limit: int | None = None) -> list:
        """Every vessel heard, freshest first, optionally capped at `limit`."""
        rows = sorted(self._by_mmsi.values(), key=lambda v: v.last_seen, reverse=True)
        return rows[:limit] if limit else rows


class _Feed:
    """Owns the companion and its connected UDP pair through reader retirement."""

    def __init__(self, proc: subprocess.Popen, sock: socket.socket, sender: socket.socket):
        self._proc = proc
        self._sock = sock
        self._sender = sender
        self._closed = threading.Event()
        self._transport_lock = threading.Lock()
        self._rejections = Counter()
        self._foreign_port = None
        # Preserve the real argv for diagnostics and reception provenance.
        self.procs = (proc,)
        self.argvs = [proc.args]
        self.stdout = self._lines()

    def configure(self):
        self._peer = self._sender.getsockname()
        self._sock.settimeout(FEED_POLL_S)

    def rejected(self):
        with self._transport_lock:
            return dict(self._rejections), self._foreign_port

    def _reject(self, reason, peer):
        with self._transport_lock:
            if self._closed.is_set():
                return
            self._rejections[reason] += 1
            if reason == "foreign":
                if self._foreign_port is None:
                    self._foreign_port = peer[1]
                return
        raise AisFeedError(
            f"{AIS_COMPANION} sent an unreadable datagram ({reason}); run deploy/provision.sh")

    def _lines(self):
        draining = False
        while not self._closed.is_set():
            try:
                if not draining and self._proc.poll() is not None:
                    # Read what the child left queued without waiting out the live poll.
                    self._sock.settimeout(0.0)
                    draining = True
                data, _, flags, peer = self._sock.recvmsg(4096)
            except TimeoutError:
                if draining:
                    return
                # If the child exited during this timeout, recheck and drain on the next pass.
                continue
            except BlockingIOError:
                if draining:
                    return
                continue
            except OSError as exc:
                if self._closed.is_set():
                    return
                raise AisFeedError(f"cannot read {AIS_COMPANION} UDP output: {exc}") from exc
            if self._closed.is_set():
                return
            # connect() filters future traffic but may leave earlier datagrams queued.
            if peer != self._peer:
                self._reject("foreign", peer)
                continue
            if flags & socket.MSG_TRUNC:
                self._reject("truncated", peer)
                return
            try:
                lines = data.decode("ascii").splitlines()
            except UnicodeDecodeError:
                lines = []
            if (not lines or not data.endswith(b"\n")
                    or any(parse_ais_sentence(line) is None for line in lines)):
                self._reject("framing", peer)
                return
            yield from lines

    def poll(self):
        return self._proc.poll()

    def close_transport(self) -> None:
        """Wake and close the receiver, then release the sender's reserved port."""
        with self._transport_lock:
            self._closed.set()
            try:
                if self._sock.fileno() != -1:
                    try:
                        self._sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self._close(self._sock)
            finally:
                self._close(self._sender)

    @staticmethod
    def _close(sock):
        if sock.fileno() != -1:
            try:
                sock.close()
            except OSError:
                if sock.fileno() != -1:
                    raise

    def stop_child(self) -> bool:
        """Stop and reap rtl_ais while leaving the UDP socket open to drain queued datagrams."""
        return stop_process(self._proc)

    def terminate(self):
        self.close_transport()
        self._proc.terminate()

    def kill(self):
        self.close_transport()
        self._proc.kill()

    def wait(self, timeout=None):
        # A timeout is passed on, not swallowed: it is what tells stop() to escalate to kill.
        return self._proc.wait(timeout=timeout)


def _check_host(host):
    if host != DEFAULT_AIS_HOST:
        raise AisUnavailable(f"AIS needs the IPv4 loopback address {DEFAULT_AIS_HOST}")


def _popen(device: int, host: str = DEFAULT_AIS_HOST, port: int = DEFAULT_AIS_PORT,
           *, binary=AIS_COMPANION) -> _Feed:
    """Give the child only its sender and retain both sockets under lifecycle ownership."""
    _check_host(host)
    with ExitStack() as sockets:
        # Reserve the source tuple before there is any receiver queue. Nobody else can
        # use that source while the console is able to admit or drain its datagrams.
        sender = sockets.enter_context(bind_receiver((host, 0), socket.SOCK_DGRAM))
        if sender.getsockname()[1] == port:
            alternate = sockets.enter_context(bind_receiver((host, 0), socket.SOCK_DGRAM))
            sender.close()
            sender = alternate
        try:
            sock = sockets.enter_context(
                bind_receiver((host, port), socket.SOCK_DGRAM, occupied=AisPortOccupied))
        except OSError as exc:
            raise AisUnavailable(f"cannot bind UDP port {host}:{port} for AIS: {exc}") from exc
        sock.connect(sender.getsockname())
        sender.connect(sock.getsockname())
        proc = spawn([binary, AIS_MODE, "-d", str(device)],
                     stdin=subprocess.DEVNULL, stdout=sender)
        feed = _Feed(proc, sock, sender)
        sockets.pop_all()
    try:
        feed.configure()
        feed.shell_command = shlex.join([RTL_AIS, "-d", str(device), "-h", host, "-P", str(port)])
    except Exception as exc:
        raise PartialLaunch(feed, exc) from exc
    return feed


class AisDecoder(SupervisedReceiver):
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
        self._capability = None
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._roster = VesselRoster()
        # Called with a vessel's dict the first time each MMSI is heard since the feed (re)started
        # — a new contact, not every position report. From the reader thread only and outside the
        # roster lock. The console's hook logs it as a reception and never raises, so the call is
        # made directly: it cannot end the decode.
        self.on_reception = None
        self.on_foreign_datagrams = None
        self._runs = RunGuard()
        self._init_lifecycle()
        self.error = None
        # Who ended the child, and when the console may launch it again after it died on its own.
        self.watch = ChildWatch(AIS_COMPANION)

    _unavailable = AisUnavailable
    _released_reason = "the radio was released to another program; resume AIS to take it back"

    def _clear(self):
        self._roster = VesselRoster()

    def _choose_radio(self) -> dict:
        try:
            return rtlsdr_radio_for_run(self.device, self.serial, self._list_devices)
        except DeviceSelectionError as exc:
            raise AisUnavailable(str(exc)) from exc

    def _companion(self):
        binary = self._which(AIS_COMPANION)
        if not binary:
            raise AisUnavailable(f"{AIS_COMPANION} is not installed; run deploy/provision.sh")
        try:
            stat = os.stat(binary)
        except OSError as exc:
            reason = f"cannot check {AIS_COMPANION}: {exc}; run deploy/provision.sh"
            raise AisUnavailable(reason) from exc
        key = stat.st_dev, stat.st_ino, stat.st_mtime_ns
        if self._capability is None or self._capability[0] != key:
            reason = None
            try:
                result = subprocess.run([binary, AIS_CAPABILITY_QUERY], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        timeout=AIS_CAPABILITY_TIMEOUT_S)
                if result.returncode or result.stdout != AIS_CAPABILITY:
                    reason = "did not return the required version"
            except subprocess.TimeoutExpired:
                # subprocess.run kills the query and reaps it before returning this exception.
                reason = "timed out"
            except OSError as exc:
                reason = str(exc)
            self._capability = key, reason
        if reason := self._capability[1]:
            raise AisUnavailable(
                f"{AIS_COMPANION} {AIS_CAPABILITY_QUERY} {reason}; run deploy/provision.sh")
        return binary

    def _prepare_launch(self, _freq_hz, _number):
        launcher = self._spawn
        host, port = self.host, self.port
        if launcher is _popen:
            _check_host(host)
            binary = self._companion()
            kwargs = {"binary": binary}
        else:
            if not self._which(RTL_AIS):
                raise AisUnavailable(f"{RTL_AIS} is not installed, so SIGINT 101 cannot decode AIS")
            kwargs = {}
        if not self._radio_present():
            raise AisUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a vessel")
        radio = self._choose_radio()
        if launcher is _popen:
            try:
                with bind_receiver((host, port), socket.SOCK_DGRAM, occupied=AisPortOccupied):
                    pass
            except OSError as exc:
                raise AisUnavailable(f"cannot check UDP port {host}:{port}: {exc}") from exc

        def launch():
            try:
                proc = launcher(radio["index"], host, port, **kwargs)
            except (OSError, ValueError) as exc:
                raise AisUnavailable(f"cannot run {AIS_COMPANION}: {exc}") from exc
            self.device = radio["index"]
            return proc

        return launch, (radio,)

    def live_command(self):
        if command := getattr(self._proc, "shell_command", None):
            return command if self.running else None
        return super().live_command()

    @property
    def rejections(self):
        run = self._run
        return run.proc.rejected()[0] if run and isinstance(run.proc, _Feed) else {}

    def _report(self, run):
        if self.on_foreign_datagrams is not None and isinstance(run.proc, _Feed):
            counts, source_port = run.proc.rejected()
            if count := counts.get("foreign"):
                self.on_foreign_datagrams(count, source_port)
        super()._report(run)

    def _read(self, proc, run: int, radio: dict) -> None:
        """Consume rtl_ais's NMEA sentences on a thread, building the roster until it ends."""
        # Each reader owns its own multi-part assembler so a half message left by a retiring
        # reader can never combine with the new run's second half.
        assembler = AisAssembler()
        contacts = set()   # the MMSIs this feed has already reported as new contacts
        try:
            for raw in proc.stdout:
                report = assembler.push(raw)
                if report is None:
                    continue
                with self._runs:
                    if not self._runs.is_current(run):
                        continue
                    vessel = self._roster.absorb(report, now=time.time())
                    contact = vessel.as_dict() if report.mmsi not in contacts else None
                    cmd = command_line(proc)
                    hook = self._admit() if contact is not None else None
                if contact is not None:
                    contacts.add(report.mmsi)
                    self._emit(hook, contact, command=cmd,
                               freq_hz=[AIS_CHANNEL_A_HZ, AIS_CHANNEL_B_HZ], radio=radio)
        except AisFeedError as exc:
            # Cancel via the supervisor so the reader never waits for its own retirement.
            with self._life:
                with self._runs:
                    if not self._runs.is_current(run):
                        return
                    self.error = str(exc)
                    # A fault in the last queued datagram explains a concurrent child exit.
                    self._run.unreported_death = False
                self._cancel()
        except (OSError, ValueError) as exc:  # loud, never silent
            with self._runs:
                if self._runs.is_current(run):
                    self.error = str(exc)

    def vessels(self, limit: int | None = None) -> list:
        """The roster so far, as plain dicts for the API, freshest vessel first."""
        with self._runs:
            rows = self._roster.vessels(limit)
        return [v.as_dict() for v in rows]
