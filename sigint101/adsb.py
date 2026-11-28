# ABOUTME: Decodes ADS-B at 1090 MHz via dump1090-fa for M13 — aircraft you never see, on a radar.
# ABOUTME: No radio, no dump1090, no planes — reports an empty sky, never invents an aircraft.

from __future__ import annotations

import errno
import math
import os
import select
import shutil
import socket
import struct
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .childproc import (
    ChildWatch,
    RunGuard,
    command_line,
    spawn,
    stderr_tail,
    stop_and_confirm,
)
from .hardware import (
    DUMP1090,
    DeviceSelectionError,
    probe_rtlsdr,
    rtlsdr_radio_for_run,
)
from .receiver_lifecycle import PartialLaunch, SupervisedReceiver
from .receiver_socket import PortOccupied, bind_receiver

# Where the ADS-B lesson listens. Mode S transponders answer on 1090 MHz worldwide; dump1090 is
# fixed to it, so this is shown to the learner rather than tuned.
DEFAULT_ADSB_HZ = 1_090_000_000

# dump1090's BaseStation (SBS-1) output port. One CSV line per decoded message; the decoder
# connects here as a client while dump1090 holds the dongle.
SBS_PORT = 30003
SBS_HOST = "127.0.0.1"
SBS_STARTUP_S = 5.0

# One arcminute of a great circle is one nautical mile, so the Earth's mean radius expressed in
# nautical miles is (180 * 60 / pi). Range comes out in nm, the unit a radar scope is read in.
_EARTH_RADIUS_NM = 180.0 * 60.0 / math.pi

# How long an aircraft stays on the scope after its last message. Mode S squitters arrive every
# second or two; a minute of silence means it flew out of range or landed.
DEFAULT_MAX_AGE_S = 60.0

# How often the feed's reader wakes from a quiet socket to check whether it was stopped or
# dump1090 has died. A quiet sky sends nothing for seconds at a time; that is not an ended feed.
FEED_POLL_S = 1.0


class AdsbUnavailable(RuntimeError):
    """The ADS-B feed cannot be read. Never a reason to invent traffic — the scope stays empty."""


class AdsbPortOccupied(AdsbUnavailable, PortOccupied):
    """dump1090's local SBS output port cannot be bound."""


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

    def absorb(self, msg: SBSMessage | None, now: float) -> Aircraft | None:
        """Fold one message into its aircraft's entry, and return that aircraft as it now stands."""
        if msg is None:
            return None
        plane = self._by_hex.get(msg.hex_ident)
        if plane is None:
            plane = Aircraft(hex_ident=msg.hex_ident)
            self._by_hex[msg.hex_ident] = plane
        plane.absorb(msg, now)
        return plane

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

    def __init__(self, proc: subprocess.Popen, sock: socket.socket | None = None):
        self._proc = proc
        self._sock = sock
        self._closed = threading.Event()
        self._transport_lock = threading.Lock()
        self._transport_closed = False
        # The one process, and its argv, for the exit reason and the command strip.
        self.procs = (proc,)
        self.argvs = [proc.args]
        self.stdout = self._lines()

    def _lines(self):
        buffer = ""
        draining = False
        try:
            while not self._closed.is_set():
                if not draining and self._proc.poll() is not None:
                    # Once dump1090 has exited, switch to non-blocking so complete lines already
                    # queued in the socket drain at once without waiting out FEED_POLL_S.
                    try:
                        self._sock.settimeout(0.0)
                    except OSError:
                        return
                    draining = True
                try:
                    chunk = self._sock.recv(4096)
                except TimeoutError:
                    if draining:
                        return
                    # No aircraft for a moment is an empty sky, not an ended feed: keep listening
                    # while dump1090 lives. If it just exited, loop once more in non-blocking mode
                    # to drain anything queued at the boundary, then return.
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk.decode("ascii", "replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    yield line
        finally:
            self._close_socket()

    def poll(self):
        return self._proc.poll()

    def _close_socket(self):
        """Let go of the SBS connection and wake a reader blocked in recv."""
        with self._transport_lock:
            if self._transport_closed or self._sock is None:
                return
            self._closed.set()
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                if self._sock.fileno() != -1:
                    raise
            self._transport_closed = True

    def stop_child(self, *, kill_only, wait_s):
        """Stop and reap dump1090 while leaving the SBS socket open to drain queued lines."""
        return stop_and_confirm(self._proc, kill_only=kill_only, wait_s=wait_s)

    def close_transport(self) -> None:
        """Close the SBS socket once any natural-exit drain has finished or timed out."""
        self._close_socket()

    def terminate(self):
        self._close_socket()
        self._proc.terminate()

    def kill(self):
        self._close_socket()
        self._proc.kill()

    def wait(self, timeout=None):
        # A timeout is passed on, not swallowed: it is what tells stop() to escalate to kill.
        return self._proc.wait(timeout=timeout)


def _probe_sbs_port():
    # Match dump1090's bind options so a previous run's TIME_WAIT does not look occupied.
    try:
        with bind_receiver((SBS_HOST, SBS_PORT), socket.SOCK_STREAM, reuse=True,
                           occupied=AdsbPortOccupied):
            pass
    except OSError as exc:
        raise AdsbUnavailable(f"cannot check TCP port {SBS_HOST}:{SBS_PORT}: {exc}") from exc


def _tcp_endpoint(endpoint, family):
    host, port = endpoint
    if family == socket.AF_INET6:
        host = f"::ffff:{host}"
    packed = socket.inet_pton(family, host)
    # Linux prints each address word in native byte order, the port as its numeric value.
    address = "".join(f"{word:08X}" for (word,) in struct.iter_unpack("=I", packed))
    return f"{address}:{port:04X}"


def _sbs_inode(sock):
    server, client = sock.getpeername(), sock.getsockname()
    for table, family in (("tcp", socket.AF_INET), ("tcp6", socket.AF_INET6)):
        endpoints = [_tcp_endpoint(addr, family) for addr in (server, client)]
        try:
            with open(f"/proc/net/{table}", encoding="ascii") as rows:
                for row in rows:
                    fields = row.split()
                    if fields[1:4] == [*endpoints, "01"] and fields[9] != "0":
                        return fields[9]
        except FileNotFoundError:
            if family == socket.AF_INET:
                raise


def _child_has_socket(pid, inode):
    with os.scandir(f"/proc/{pid}/fd") as entries:
        for entry in entries:
            try:
                if os.readlink(entry.path) == f"socket:[{inode}]":
                    return True
            except FileNotFoundError:  # The child can close an unrelated descriptor.
                continue
    return False


def _pidfd_exited(pidfd):
    ready = select.poll()
    ready.register(pidfd, select.POLLIN)
    return bool(ready.poll(0))


@contextmanager
def _sbs_peer(proc):
    # Open the identity handle before any poll can reap the child.
    open_pidfd = getattr(os, "pidfd_open", None)
    pidfd = None
    if open_pidfd is not None:
        try:
            pidfd = open_pidfd(proc.pid)
        except OSError as exc:
            if exc.errno not in {errno.ENOSYS, errno.EINVAL, errno.ENODEV, errno.EOPNOTSUPP,
                                 errno.EPERM, errno.EACCES}:
                raise AdsbUnavailable(f"cannot open dump1090 pidfd: {exc}") from exc

    def ended():
        tail = "; ".join(stderr_tail(proc))
        reason = "dump1090 exited during SBS ownership verification"
        raise AdsbUnavailable(f"{reason}: {tail}" if tail else reason)

    def matches(sock):
        inode = _sbs_inode(sock)
        return bool(inode and _child_has_socket(proc.pid, inode) and _sbs_inode(sock) == inode)

    def verify(sock, check):
        check()
        try:
            return matches(sock)
        finally:
            # A dying child can lose its fd directory midway through the scan. In that
            # case report the child exit and its stderr before the procfs lookup error.
            check()

    def with_pidfd(sock):
        def check():
            if _pidfd_exited(pidfd):
                ended()

        return verify(sock, check)

    def with_wait_lock(sock):
        def check():
            if (proc.returncode is not None or proc._exit_pending() is not None
                    or proc.returncode is not None):
                ended()

        # C42's wait lock keeps a managed reap from releasing the PID during the lookup.
        # Hold it only for this observation. Connection and retry waits stay outside.
        with proc._waitpid_lock:
            return verify(sock, check)

    try:
        yield with_pidfd if pidfd is not None else with_wait_lock
    finally:
        if pidfd is not None:
            os.close(pidfd)


def _connect_sbs(feed, deadline, owns_peer):
    # A connection can arrive before accept installs the server's fd. Keep the socket and
    # retry the proof against it, with no reader attached until the child owns it.
    while True:
        if feed._proc.poll() is not None:
            tail = "; ".join(stderr_tail(feed._proc))
            reason = "dump1090 exited before opening its SBS feed"
            raise AdsbUnavailable(f"{reason}: {tail}" if tail else reason)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = ("did not open its SBS port" if feed._sock is None
                      else "could not verify ownership of its SBS connection")
            raise AdsbUnavailable(f"dump1090 {reason} at {SBS_HOST}:{SBS_PORT}")
        if feed._sock is None:
            try:
                feed._sock = socket.create_connection((SBS_HOST, SBS_PORT),
                                                      timeout=min(1.0, remaining))
            except (ConnectionRefusedError, TimeoutError):
                pass
        if (feed._sock is not None and owns_peer(feed._sock)
                and time.monotonic() < deadline):
            feed._sock.settimeout(FEED_POLL_S)
            return
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _popen(device: int) -> _Feed:
    """Launch dump1090 on 1090 MHz and attach only to its own SBS connection."""
    deadline = time.monotonic() + SBS_STARTUP_S
    proc = spawn(
        [DUMP1090, "--device-index", str(device), "--net", "--net-bind-address", SBS_HOST,
         "--net-ri-port", "0", "--net-bi-port", "0",
         "--net-ro-port", "0", "--net-bo-port", "0",
         "--net-sbs-port", str(SBS_PORT), "--quiet"],
        stdout=subprocess.DEVNULL)
    # Keep the actual child and any candidate socket under one owner from spawn onward.
    feed = _Feed(proc)
    try:
        with _sbs_peer(proc) as owns_peer:
            _connect_sbs(feed, deadline, owns_peer)
    except Exception as exc:
        # C40 fences the reader and retains the process even if teardown also fails.
        raise PartialLaunch(feed, exc) from exc
    return feed


@dataclass
class _Receiver:
    lat: float | None = None
    lon: float | None = None

    @property
    def known(self) -> bool:
        return self.lat is not None and self.lon is not None


class AdsbDecoder(SupervisedReceiver):
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
        # Called with an aircraft's dict the first time each ICAO address is heard since the feed
        # (re)started — a new contact, not every squitter. From the reader thread only and outside
        # the tracker lock. The console's hook logs it as a reception and never raises, so the
        # call is made directly: it cannot end the decode.
        self.on_reception = None
        self._runs = RunGuard()
        self._init_lifecycle()
        self.error = None
        # Who ended the child, and when the console may launch it again after it died on its own.
        self.watch = ChildWatch(DUMP1090)

    _unavailable = AdsbUnavailable
    _released_reason = "the radio was released to another program; resume ADS-B to take it back"

    def _clear(self):
        self._tracker = AircraftTracker()

    def _choose_radio(self) -> dict:
        try:
            return rtlsdr_radio_for_run(self.device, self.serial, self._list_devices)
        except DeviceSelectionError as exc:
            raise AdsbUnavailable(str(exc)) from exc

    def _prepare_launch(self, _freq_hz, _number):
        if not self._which(DUMP1090):
            raise AdsbUnavailable(
                f"{DUMP1090} is not installed, so SIGINT 101 cannot decode ADS-B")
        if not self._radio_present():
            raise AdsbUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake traffic")
        radio = self._choose_radio()
        launcher = self._spawn
        if launcher is _popen:
            _probe_sbs_port()

        def launch():
            try:
                proc = launcher(radio["index"])
            except (OSError, ValueError) as exc:
                raise AdsbUnavailable(f"cannot run dump1090: {exc}") from exc
            self.device = radio["index"]
            return proc

        return launch, (radio,)

    def _read(self, proc, run: int, radio: dict) -> None:
        """Consume dump1090's SBS lines on a background thread, assembling the sky until it ends."""
        contacts = set()   # the ICAO addresses this feed has already reported as new contacts
        try:
            for raw in proc.stdout:
                msg = parse_sbs_message(raw)
                if msg is None:
                    continue
                with self._runs:
                    if not self._runs.is_current(run):
                        continue
                    plane = self._tracker.absorb(msg, now=time.time())
                    contact = self._row(plane) if msg.hex_ident not in contacts else None
                    cmd = command_line(proc)
                    hook = self._admit() if contact is not None else None
                if contact is not None:
                    contacts.add(msg.hex_ident)
                    self._emit(hook, contact, command=cmd, freq_hz=DEFAULT_ADSB_HZ, radio=radio)
        except (OSError, ValueError) as exc:  # loud, never silent
            with self._runs:
                if self._runs.is_current(run):
                    self.error = str(exc)

    def aircraft(self) -> list[dict]:
        """The live sky as plain dicts for the API, closest-ranged aircraft first when located."""
        now = time.time()
        with self._runs:
            planes = self._tracker.aircraft(now=now, max_age_s=self.max_age_s)
        out = [self._row(p) for p in planes]
        # Closest first when ranges are known; the located aircraft lead the unlocated.
        out.sort(key=lambda a: (a["range_nm"] is None, a["range_nm"] or 0.0))
        return out

    def _row(self, p: Aircraft) -> dict:
        """One aircraft as the API hands it out, placed by range and bearing when located."""
        range_nm = bearing_deg = None
        if self.receiver.known and p.lat is not None and p.lon is not None:
            range_nm, bearing_deg = great_circle(
                self.receiver.lat, self.receiver.lon, p.lat, p.lon)
        return {
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
        }
