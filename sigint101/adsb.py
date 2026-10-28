# ABOUTME: Decodes ADS-B at 1090 MHz via dump1090-fa for M13 — aircraft you never see, on a radar.
# ABOUTME: No radio, no dump1090, no planes — reports an empty sky, never invents an aircraft.

from __future__ import annotations

import math
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass

from .hardware import (
    DUMP1090,
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# Where the ADS-B lesson listens. Mode S transponders answer on 1090 MHz worldwide; dump1090 is
# fixed to it, so this is shown to the learner rather than tuned.
DEFAULT_ADSB_HZ = 1_090_000_000

# dump1090's BaseStation (SBS-1) output port. One CSV line per decoded message; the decoder
# connects here as a client while dump1090 holds the dongle.
SBS_PORT = 30003

# One arcminute of a great circle is one nautical mile, so the Earth's mean radius expressed in
# nautical miles is (180 * 60 / pi). Range comes out in nm, the unit a radar scope is read in.
_EARTH_RADIUS_NM = 180.0 * 60.0 / math.pi

# How long an aircraft stays on the scope after its last message. Mode S squitters arrive every
# second or two; a minute of silence means it flew out of range or landed.
DEFAULT_MAX_AGE_S = 60.0


class AdsbUnavailable(RuntimeError):
    """The ADS-B feed cannot be read. Never a reason to invent traffic — the scope stays empty."""


def _to_float(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(text: str):
    # A geometric altitude is printed with a trailing 'H'; it is not part of the number.
    text = (text or "").strip().rstrip("Hh").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class SBSMessage:
    """One decoded Mode S message, as dump1090 reports it in BaseStation format.

    A single message carries only part of an aircraft's picture — an identity message has the
    callsign, a position message the altitude and lat/lon, a velocity message the speed and
    track — so the tracker is what assembles a whole aircraft from the stream.
    """

    msg_type: int
    hex_ident: str
    callsign: str | None = None
    altitude_ft: int | None = None
    ground_speed_kt: float | None = None
    track_deg: float | None = None
    lat: float | None = None
    lon: float | None = None
    squawk: str | None = None


def parse_sbs_message(line: str) -> SBSMessage | None:
    """Turn one dump1090 BaseStation line into an SBSMessage, or None if it is not one.

    dump1090 also emits STA/ID/AIR/SEL/CLK status records; only MSG transmission records carry
    aircraft data. A line without the 22 BaseStation fields, or with no ICAO hex address, is not
    an aircraft message to absorb.
    """
    text = (line or "").strip()
    if not text.startswith("MSG,"):
        return None
    fields = text.split(",")
    if len(fields) < 22:
        return None
    try:
        msg_type = int(fields[1])
    except ValueError:
        return None
    hex_ident = fields[4].strip().upper()
    if not hex_ident:
        return None
    callsign = fields[10].strip() or None
    return SBSMessage(
        msg_type=msg_type,
        hex_ident=hex_ident,
        callsign=callsign,
        altitude_ft=_to_int(fields[11]),
        ground_speed_kt=_to_float(fields[12]),
        track_deg=_to_float(fields[13]),
        lat=_to_float(fields[14]),
        lon=_to_float(fields[15]),
        squawk=fields[17].strip() or None,
    )


def great_circle(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Range (nautical miles) and initial bearing (degrees from true north) from point 1 to 2.

    Haversine for the distance, the standard forward-azimuth formula for the bearing. This is
    what places an aircraft on the scope: the range sets its ring, the bearing its angle.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    distance = _EARTH_RADIUS_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return distance, bearing


@dataclass
class Aircraft:
    """One aircraft assembled from the stream of messages that share its ICAO hex address."""

    hex_ident: str
    callsign: str | None = None
    altitude_ft: int | None = None
    ground_speed_kt: float | None = None
    track_deg: float | None = None
    lat: float | None = None
    lon: float | None = None
    squawk: str | None = None
    message_count: int = 0
    last_seen: float = 0.0

    def absorb(self, msg: SBSMessage, now: float) -> None:
        """Fold one message in, keeping each field from the last message that carried it."""
        self.message_count += 1
        self.last_seen = now
        for name in ("callsign", "altitude_ft", "ground_speed_kt",
                     "track_deg", "lat", "lon", "squawk"):
            value = getattr(msg, name)
            if value is not None:
                setattr(self, name, value)


class AircraftTracker:
    """The live sky: every aircraft currently being heard, assembled from the SBS stream.

    Unlike the RDS station (one transmitter) this keeps many aircraft at once, keyed by ICAO hex,
    and drops any it has not heard from recently — a scope must show what is up there now, not a
    memory of what has already flown past.
    """

    def __init__(self):
        self._by_hex: dict[str, Aircraft] = {}

    def absorb(self, msg: SBSMessage | None, now: float) -> None:
        if msg is None:
            return
        plane = self._by_hex.get(msg.hex_ident)
        if plane is None:
            plane = Aircraft(hex_ident=msg.hex_ident)
            self._by_hex[msg.hex_ident] = plane
        plane.absorb(msg, now)

    def aircraft(self, now: float, max_age_s: float = DEFAULT_MAX_AGE_S) -> list[Aircraft]:
        """Every aircraft heard within the age window, freshest contact first."""
        live = [p for p in self._by_hex.values() if now - p.last_seen <= max_age_s]
        return sorted(live, key=lambda p: p.last_seen, reverse=True)


class _Feed:
    """The live dump1090 process and the SBS socket as one object: lines to read, both to tear down.

    dump1090 holds the dongle and serves BaseStation text on a TCP port; this connects as a client
    and exposes the socket as an iterator of lines, so the decoder reads it exactly as it would a
    subprocess's stdout.
    """

    def __init__(self, proc: subprocess.Popen, sock: socket.socket):
        self._proc = proc
        self._sock = sock
        self.stdout = self._lines()

    def _lines(self):
        buffer = ""
        with self._sock:
            while True:
                try:
                    chunk = self._sock.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk.decode("ascii", "replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
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


def _popen(device: int) -> _Feed:
    """Launch dump1090 on 1090 MHz serving SBS, and connect to its BaseStation port."""
    proc = subprocess.Popen(
        [DUMP1090, "--device-index", str(device), "--net",
         "--net-sbs-port", str(SBS_PORT), "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # dump1090 needs a moment to grab the dongle and open its listener.
    deadline = time.time() + 5.0
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", SBS_PORT), timeout=1.0)
            break
        except OSError:
            if time.time() > deadline or proc.poll() is not None:
                try:
                    proc.terminate()
                except (OSError, subprocess.SubprocessError):
                    pass
                raise AdsbUnavailable(
                    "dump1090 did not open its SBS port — cannot read the ADS-B feed")
            time.sleep(0.2)
    return _Feed(proc, sock)


@dataclass
class _Receiver:
    lat: float | None = None
    lon: float | None = None

    @property
    def known(self) -> bool:
        return self.lat is not None and self.lon is not None


class AdsbDecoder:
    """Owns one dump1090 feed and the live sky it assembles.

    A sibling of the RDS decoder and the rtl_433 decoder: an SDR-exclusive tool with a reader
    thread off the request path and release/resume so it takes turns with the waterfall on the one
    dongle. A receiver location, when set, turns each aircraft's lat/lon into a range and bearing
    so it can be placed on the scope.
    """

    def __init__(self, *, device=0, spawn=_popen, radio_present=None, which=shutil.which,
                 receiver_lat=None, receiver_lon=None, serial=None, list_devices=None,
                 max_age_s=DEFAULT_MAX_AGE_S):
        self.device = device
        self.serial = serial or None
        self.receiver = _Receiver(receiver_lat, receiver_lon)
        self.max_age_s = max_age_s
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._tracker = AircraftTracker()
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
            raise AdsbUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed."""
        with self._life:
            self._released = True
            self.stop()
            with self._lock:
                self._tracker = AircraftTracker()

    def resume(self) -> None:
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin decoding ADS-B, unless the radio is absent, dump1090 is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        if self._released:
            raise AdsbUnavailable(
                "the radio was released to another program; resume ADS-B to take it back")
        if not self._which(DUMP1090):
            raise AdsbUnavailable(
                f"{DUMP1090} is not installed, so SIGINT 101 cannot decode ADS-B")
        if not self._radio_present():
            raise AdsbUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake traffic")
        if self.error:
            reason, self.error = self.error, None
            raise AdsbUnavailable(reason)
        self.device = self._choose_device()
        try:
            self._proc = self._spawn(self.device)
        except AdsbUnavailable:
            self._proc = None
            raise
        except (OSError, ValueError) as exc:
            self._proc = None
            raise AdsbUnavailable(f"cannot run dump1090: {exc}") from exc
        self.error = None
        self._thread = threading.Thread(target=self._read, name="sigint101-adsb", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Consume dump1090's SBS lines on a background thread, assembling the sky until it ends."""
        try:
            for raw in self._proc.stdout:
                msg = parse_sbs_message(raw)
                if msg is not None:
                    with self._lock:
                        self._tracker.absorb(msg, now=time.time())
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
                self.error = "dump1090 did not stop; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. A real ADS-B feed never ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def aircraft(self) -> list[dict]:
        """The live sky as plain dicts for the API, closest-ranged aircraft first when located."""
        now = time.time()
        with self._lock:
            planes = self._tracker.aircraft(now=now, max_age_s=self.max_age_s)
        out = []
        for p in planes:
            range_nm = bearing_deg = None
            if self.receiver.known and p.lat is not None and p.lon is not None:
                range_nm, bearing_deg = great_circle(
                    self.receiver.lat, self.receiver.lon, p.lat, p.lon)
            out.append({
                "hex_ident": p.hex_ident,
                "callsign": p.callsign,
                "altitude_ft": p.altitude_ft,
                "ground_speed_kt": p.ground_speed_kt,
                "track_deg": p.track_deg,
                "lat": p.lat,
                "lon": p.lon,
                "squawk": p.squawk,
                "message_count": p.message_count,
                "range_nm": range_nm,
                "bearing_deg": bearing_deg,
            })
        # Closest first when ranges are known; the located aircraft lead the unlocated.
        out.sort(key=lambda a: (a["range_nm"] is None, a["range_nm"] or 0.0))
        return out
