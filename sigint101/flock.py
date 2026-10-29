# ABOUTME: Detects Flock ALPR cameras on nomad-b's wlan1 — parses 802.11 mgmt frames, matches sigs.
# ABOUTME: No monitor interface, no tcpdump, no camera in range — an empty roster, never a fake hit.

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import threading
import time
from dataclasses import dataclass

# The monitor interface the detector listens on. wlan1 (RTL8821CU) is a separate radio from the
# SDR dongle, so there is no release/resume arbitration — this owns wlan1 the way meshtasticd owns
# LoRa. A deployment with a different adapter name passes interface= to the detector.
DEFAULT_MONITOR_INTERFACE = "wlan1"

# The capture program. tcpdump streams a live pcap of management frames to stdout; the detector
# parses that stream rather than shelling out per packet.
TCPDUMP = "tcpdump"

# pcap stream framing: a 24-byte global header, then per record a 16-byte header of four little-
# endian uint32s (ts_sec, ts_usec, caplen, origlen) followed by caplen bytes of captured frame.
PCAP_GLOBAL_HEADER_LEN = 24
PCAP_RECORD_HEADER_LEN = 16

# 802.11 frame control: type and subtype live in the first octet. A probe request is management
# (type 0), subtype 4. A management frame header is 24 bytes: fc(2) dur(2) addr1(6) addr2(6)
# addr3(6) seq(2); the information elements of a probe request begin right after it.
FRAME_TYPE_MANAGEMENT = 0
SUBTYPE_PROBE_REQUEST = 4
MGMT_HEADER_LEN = 24

# The Flock OUI set from colonelpanichacks/flock-you @6704f01 (main.cpp): 32 community-reported
# plus 2 firmware-extracted (b4:1e:52 is Flock's own IEEE block, 00:03:7f the Qualcomm Atheros
# chip in the camera). Lowercase, matched against the first three octets of a MAC. The locally-
# administered 82:6b:f2 is kept deliberately — a set bit in the first octet does not exempt it.
FLOCK_OUIS = frozenset({
    "70:c9:4e", "3c:91:80", "d8:f3:bc", "80:30:49", "b8:35:32", "14:5a:fc", "74:4c:a1",
    "08:3a:88", "9c:2f:9d", "c0:35:32", "94:08:53", "e4:aa:ea", "f4:6a:dd", "e0:0a:f6",
    "24:b2:b9", "00:f4:8d", "d0:39:57", "e8:d0:fc", "e0:4f:43", "b8:1e:a4", "70:08:94",
    "58:8e:81", "ec:1b:bd", "3c:71:bf", "58:00:e3", "90:35:ea", "5c:93:a2", "64:6e:69",
    "48:27:ea", "a4:cf:12", "14:b5:cd", "82:6b:f2",
    "b4:1e:52", "00:03:7f",
})

# The Flock probe-request IE fingerprint: walk the TLVs in order, skip the SSID (tag 0), emit each
# non-vendor tag as its decimal id and each vendor IE (tag 221) as "221:" + up to its first eight
# payload bytes in lowercase hex. This exact string is the camera's probe-request signature.
FLOCK_IE_SIGNATURE = "2,12,127,221:506f9a16030103,45,191,221:0050f208000000"

# Case-insensitive SSID substrings. Lowest tier and off by default: an SSID is attacker-settable,
# so a keyword alone is the weakest of evidence and must be opted into, never assumed.
FLOCK_SSID_KEYWORDS = ("flock", "penguin", "fs ext battery")

VENDOR_SPECIFIC_TAG = 221
SSID_TAG = 0


class FlockUnavailable(RuntimeError):
    """The capture cannot be read. Never a reason to invent a camera — the roster stays empty."""


@dataclass(frozen=True)
class FrameInfo:
    """One parsed 802.11 management frame: its addresses and, for a probe request, its IEs.

    `src` is the transmitter (addr2), `dst` the receiver (addr1), `bssid` addr3. `ie_signature` is
    the probe-request fingerprint described on FLOCK_IE_SIGNATURE, or '' for any other frame.
    """

    src: str
    dst: str
    bssid: str
    ftype: int
    subtype: int
    is_probe_request: bool
    ssid: str | None
    ssid_wildcard: bool
    ie_signature: str


@dataclass(frozen=True)
class FlockHit:
    """Why a frame is believed to belong to a Flock camera: the best tier that fired, and on what.

    `mac` is the address that carried the evidence (the transmitter for tiers 2-4 and 0, the
    receiver or BSSID for tier 1); `oui` the matched OUI, or None for an SSID-keyword hit.
    """

    tier: int
    rule: str
    mac: str
    oui: str | None
    ssid: str | None


def _mac(raw: bytes) -> str:
    """Six address octets as a lowercase colon-separated string."""
    return ":".join(f"{b:02x}" for b in raw)


def _oui(mac: str) -> str:
    """The first three octets of a MAC string — its OUI."""
    return mac[:8]


def _is_multicast(mac: str) -> bool:
    """True when the group bit (the low bit of the first octet) is set — a broadcast/multicast."""
    return bool(int(mac[:2], 16) & 0x01)


def _parse_ies(ies: bytes) -> tuple[str | None, bool, str]:
    """Walk the information-element TLVs, returning (ssid, ssid_wildcard, fingerprint).

    The fingerprint skips the SSID and emits each remaining tag per FLOCK_IE_SIGNATURE. A tag whose
    declared length runs past the captured bytes (common with tcpdump's snaplen) still contributes
    its id: the slice clamps and the walk then ends, which is what the upstream detector does.
    """
    ssid: str | None = None
    ssid_wildcard = False
    parts: list[str] = []
    i = 0
    while i + 2 <= len(ies):
        tag = ies[i]
        length = ies[i + 1]
        val = ies[i + 2:i + 2 + length]
        if tag == SSID_TAG:
            ssid = val.decode("utf-8", "replace")
            ssid_wildcard = length == 0
        elif tag == VENDOR_SPECIFIC_TAG:
            parts.append("221:" + val[:8].hex())
        else:
            parts.append(str(tag))
        i += 2 + length
    return ssid, ssid_wildcard, ",".join(parts)


def parse_management_frame(dot11: bytes) -> FrameInfo | None:
    """Turn raw 802.11 bytes (radiotap already stripped) into a FrameInfo, or None.

    None when the frame is too short for a management header or is not a management frame at all;
    only management frames carry the probe requests and beacons the detector reasons about.
    """
    if len(dot11) < MGMT_HEADER_LEN:
        return None
    fc0 = dot11[0]
    ftype = (fc0 >> 2) & 0x3
    subtype = (fc0 >> 4) & 0xF
    if ftype != FRAME_TYPE_MANAGEMENT:
        return None
    dst = _mac(dot11[4:10])
    src = _mac(dot11[10:16])
    bssid = _mac(dot11[16:22])
    is_probe_request = subtype == SUBTYPE_PROBE_REQUEST
    if is_probe_request:
        ssid, ssid_wildcard, ie_signature = _parse_ies(dot11[MGMT_HEADER_LEN:])
    else:
        ssid, ssid_wildcard, ie_signature = None, False, ""
    return FrameInfo(src=src, dst=dst, bssid=bssid, ftype=ftype, subtype=subtype,
                     is_probe_request=is_probe_request, ssid=ssid,
                     ssid_wildcard=ssid_wildcard, ie_signature=ie_signature)


def _ssid_keyword(ssid: str | None) -> str | None:
    """The Flock SSID keyword contained in `ssid` (case-insensitive), or None."""
    if not ssid:
        return None
    lowered = ssid.lower()
    for keyword in FLOCK_SSID_KEYWORDS:
        if keyword in lowered:
            return keyword
    return None


def match_signatures(
    frame: FrameInfo | None, *, ssid_match_enabled: bool = False
) -> FlockHit | None:
    """The best Flock tier a frame earns, or None. Highest tier wins.

    tier 4 probe request from a Flock OUI (transmitter) whose IE fingerprint matches exactly;
    tier 3 wildcard-SSID probe request from a Flock OUI but the fingerprint does not match;
    tier 2 a Flock OUI as the transmitter of any management frame;
    tier 1 a Flock OUI as the unicast receiver, or as the BSSID of a management frame;
    tier 0 an SSID keyword match, only when enabled.
    """
    if frame is None:
        return None
    src_oui = _oui(frame.src)
    if frame.is_probe_request and src_oui in FLOCK_OUIS:
        if frame.ie_signature == FLOCK_IE_SIGNATURE:
            return FlockHit(4, "probe-request IE fingerprint", frame.src, src_oui, frame.ssid)
        if frame.ssid_wildcard:
            return FlockHit(3, "wildcard probe from Flock OUI", frame.src, src_oui, frame.ssid)
    if src_oui in FLOCK_OUIS:
        return FlockHit(2, "Flock OUI as transmitter", frame.src, src_oui, frame.ssid)
    dst_oui = _oui(frame.dst)
    if dst_oui in FLOCK_OUIS and not _is_multicast(frame.dst):
        return FlockHit(1, "Flock OUI as receiver", frame.dst, dst_oui, frame.ssid)
    bssid_oui = _oui(frame.bssid)
    if bssid_oui in FLOCK_OUIS and not _is_multicast(frame.bssid):
        return FlockHit(1, "Flock OUI as BSSID", frame.bssid, bssid_oui, frame.ssid)
    if ssid_match_enabled:
        keyword = _ssid_keyword(frame.ssid)
        if keyword is not None:
            return FlockHit(0, "SSID keyword", frame.src, None, frame.ssid)
    return None


@dataclass
class FlockDetection:
    """One detected device, assembled from every frame that implicated the same MAC.

    `tier` and `rule` track the strongest evidence ever seen; `count` and `last_seen` grow with
    each sighting, because a camera beacons and probes repeatedly rather than once.
    """

    mac: str
    oui: str | None
    tier: int
    rule: str
    ssid: str | None
    count: int = 0
    last_seen: float = 0.0

    def observe(self, hit: FlockHit, now: float) -> None:
        """Fold one hit in: always count it, and keep the strongest tier and its rule."""
        self.count += 1
        self.last_seen = now
        if hit.tier > self.tier:
            self.tier = hit.tier
            self.rule = hit.rule
            self.oui = hit.oui
        if hit.ssid:
            self.ssid = hit.ssid

    def as_dict(self) -> dict:
        return {
            "mac": self.mac,
            "oui": self.oui,
            "tier": self.tier,
            "rule": self.rule,
            "ssid": self.ssid,
            "count": self.count,
            "last_seen": self.last_seen,
        }


class FlockRoster:
    """Every detected Flock device heard so far, keyed by the MAC the evidence named.

    A sibling of the ADS-B tracker: newest sighting wins on a device's mutable fields, but unlike
    the sky this never ages a device out — a camera seen once is a camera seen.
    """

    def __init__(self):
        self._by_mac: dict[str, FlockDetection] = {}

    def absorb(self, hit: FlockHit | None, now: float) -> None:
        if hit is None:
            return
        entry = self._by_mac.get(hit.mac)
        if entry is None:
            entry = FlockDetection(mac=hit.mac, oui=hit.oui, tier=hit.tier,
                                   rule=hit.rule, ssid=hit.ssid)
            self._by_mac[hit.mac] = entry
        entry.observe(hit, now)

    def detections(self, limit: int | None = None) -> list[FlockDetection]:
        """Every detected device, most-recently-heard first, optionally capped at `limit`."""
        rows = sorted(self._by_mac.values(), key=lambda d: d.last_seen, reverse=True)
        return rows[:limit] if limit else rows


def _read_exact(stream, n: int) -> bytes:
    """Read exactly n bytes from a binary stream, or fewer at end of stream."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _strip_radiotap(frame: bytes) -> bytes | None:
    """Drop the radiotap header, returning the 802.11 bytes, or None if the header is malformed.

    radiotap: byte 0 version, byte 1 pad, bytes 2:4 the little-endian header length. The length
    varies between captures and adapters, so it is always read, never assumed.
    """
    if len(frame) < 4:
        return None
    rt_len = struct.unpack("<H", frame[2:4])[0]
    if rt_len < 4 or rt_len > len(frame):
        return None
    return frame[rt_len:]


def iter_pcap_frames(stream):
    """Yield the 802.11 bytes of each frame in a pcap stream, radiotap stripped.

    Reads the global header once, then one record at a time, so it works on a live tcpdump pipe
    exactly as on a captured file. A short read (a truncated or ended stream) stops iteration.
    """
    if len(_read_exact(stream, PCAP_GLOBAL_HEADER_LEN)) < PCAP_GLOBAL_HEADER_LEN:
        return
    while True:
        header = _read_exact(stream, PCAP_RECORD_HEADER_LEN)
        if len(header) < PCAP_RECORD_HEADER_LEN:
            return
        _, _, caplen, _ = struct.unpack("<IIII", header)
        frame = _read_exact(stream, caplen)
        if len(frame) < caplen:
            return
        dot11 = _strip_radiotap(frame)
        if dot11 is not None:
            yield dot11


def _interface_present(interface: str) -> bool:
    """True when the named network interface exists on the host."""
    return os.path.exists(f"/sys/class/net/{interface}")


def _popen(interface: str):
    """Launch tcpdump streaming a live pcap of management frames off the monitor interface."""
    return subprocess.Popen(
        [TCPDUMP, "-i", interface, "-U", "-s", "512", "-w", "-", "type mgt"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


class FlockDetector:
    """Owns the tcpdump capture on the monitor interface and the roster it assembles.

    Unlike the SDR decoders this needs no release/resume: wlan1 is its own radio, so it never
    takes turns with the waterfall. It still stops cleanly, and refuses honestly — raising
    FlockUnavailable — when tcpdump or the monitor interface is missing, rather than invent a hit.
    """

    def __init__(self, *, interface=DEFAULT_MONITOR_INTERFACE, spawn=_popen,
                 radio_present=None, which=shutil.which, ssid_match_enabled=False):
        self.interface = interface
        self.ssid_match_enabled = ssid_match_enabled
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: _interface_present(self.interface))
        self._roster = FlockRoster()
        self._lock = threading.Lock()
        self._life = threading.RLock()
        self._proc = None
        self._thread = None
        self.error = None

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self) -> None:
        """Begin capturing, unless the monitor interface is absent or tcpdump is missing."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        if not self._which(TCPDUMP):
            raise FlockUnavailable(
                f"{TCPDUMP} is not installed, so SIGINT 101 cannot watch for Flock cameras")
        if not self._radio_present():
            raise FlockUnavailable(
                f"no monitor interface {self.interface} — SIGINT 101 will not fake a detection")
        if self.error:
            reason, self.error = self.error, None
            raise FlockUnavailable(reason)
        try:
            self._proc = self._spawn(self.interface)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise FlockUnavailable(f"cannot run tcpdump: {exc}") from exc
        self.error = None
        self._thread = threading.Thread(target=self._read, name="sigint101-flock", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Consume the pcap stream on a background thread, feeding matched frames to the roster."""
        try:
            for dot11 in iter_pcap_frames(self._proc.stdout):
                frame = parse_management_frame(dot11)
                if frame is None:
                    continue
                hit = match_signatures(frame, ssid_match_enabled=self.ssid_match_enabled)
                if hit is not None:
                    with self._lock:
                        self._roster.absorb(hit, now=time.time())
        except (OSError, ValueError, struct.error) as exc:  # loud, never silent
            self.error = str(exc)

    def stop(self) -> None:
        """End the capture and release the monitor interface."""
        with self._life:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.error = "tcpdump did not stop; the monitor interface may still be held"
            except (OSError, subprocess.SubprocessError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. A real capture never ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def detections(self, limit: int | None = None) -> list[dict]:
        """The detected Flock devices as plain dicts for the API, most recently heard first."""
        with self._lock:
            rows = self._roster.detections(limit)
        return [d.as_dict() for d in rows]
