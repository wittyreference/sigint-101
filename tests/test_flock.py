# ABOUTME: Tests the Flock ALPR detector — parsing real 802.11 mgmt frames and matching signatures.
# ABOUTME: A real off-air wlan1 capture, also wrapped as Marauder's serial; tcpdump and tty faked.

import errno
import hashlib
import io
import math
import os
import re
import select
import shlex
import struct
import subprocess
import termios
import textwrap
import time
import zlib
from pathlib import Path

import pytest

import sigint101.flock as flock_module
from sigint101 import childproc
from sigint101.flock import (
    FLOCK_IE_SIGNATURE,
    FLOCK_OUIS,
    MARAUDER_TTY_GLOB,
    FlockDetector,
    FlockRoster,
    FlockUnavailable,
    MarauderFlockDetector,
    MarauderLink,
    _open_marauder_port,
    configured_flock_detector,
    iter_marauder_frames,
    iter_pcap_captures,
    iter_pcap_frames,
    match_signatures,
    parse_management_frame,
)
from tests.child_fakes import Child
from tests.test_app import _client
from tests.test_app_radio_routes import _panel_shows
from tests.test_templates import refusal_lines_shown

# A real off-air capture: tcpdump -i wlan1 -s 512 -w off wlan1 in monitor mode, 2026-10-29.
# Its network and device names, serial number, LAN address and hardware addresses were then
# replaced by made-up ones (locally administered MACs), keeping each frame's kind, order,
# timestamp and length, and recomputing its FCS. 207 management frames, 63 of them probe
# requests (example_one, Gadget_OTA, DIRECT-Auto, ...).
# No Flock ALPR camera was in range when it was taken, so every frame here is a true negative —
# the honest baseline a detector must not turn into a false alarm.
FIXTURE = Path(__file__).parent / "fixtures" / "flock_ch1.pcap"

# 34 Flock OUIs from colonelpanichacks/flock-you @6704f01 main.cpp (32 community + 2 firmware).
FLOCK_OUI = bytes.fromhex("b41e52")          # Flock's own IEEE block
FLOCK_OUI_LOCALLY_ADMIN = bytes.fromhex("826bf2")   # has the LA bit set — must still match
BROADCAST = b"\xff\xff\xff\xff\xff\xff"
NON_FLOCK = bytes.fromhex("ac:f1:08".replace(":", ""))   # a registered vendor prefix, not Flock's

# The exact Flock probe-request IE sequence, per the upstream fingerprint. Built by hand so that
# walking its TLVs (skipping the SSID) serialises to FLOCK_IE_SIGNATURE. No real camera was in
# range, so the positive case is a constructed frame in the documented upstream format.
FLOCK_IES = bytes.fromhex(
    "0000"                         # SSID, wildcard (length 0) — skipped by the fingerprint
    "020100"                       # tag 2
    "0c0100"                       # tag 12
    "7f080000000000000000"         # tag 127 (extended capabilities), 8 bytes
    "dd07506f9a16030103"           # vendor 221:506f9a16030103 (7-byte payload)
    "2d1a" + "00" * 26 +           # tag 45 (HT capabilities), 26 bytes
    "bf0c" + "00" * 12 +           # tag 191 (VHT capabilities), 12 bytes
    "dd070050f208000000"          # vendor 221:0050f208000000 (7-byte payload)
)

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def mgmt_frame(subtype, addr1, addr2, addr3, ies=b"", ftype=0):
    """Assemble an 802.11 management frame (radiotap already stripped) for the matcher tests."""
    fc0 = (subtype << 4) | (ftype << 2)
    return bytes([fc0, 0x00]) + b"\x00\x00" + addr1 + addr2 + addr3 + b"\x00\x00" + ies


def radiotap(dot11, rt_len=36):
    """Prepend a radiotap header of the given length (version 0, zero-filled fields)."""
    return struct.pack("<BBH", 0, 0, rt_len) + b"\x00" * (rt_len - 4) + dot11


def pcap_stream(frames, rt_len=36):
    """A minimal pcap byte stream (linktype 127) wrapping each dot11 frame in radiotap."""
    out = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 512, 127)
    for dot11 in frames:
        rec = radiotap(dot11, rt_len)
        out += struct.pack("<IIII", 0, 0, len(rec), len(rec)) + rec
    return out


def ssid_ies(name):
    """IEs carrying just an SSID tag, for the keyword-match tier."""
    raw = name.encode()
    return bytes([0, len(raw)]) + raw


# --------------------------------------------------------------------------- pcap + parser


def test_iter_pcap_frames_reads_every_record_from_the_capture_fixture():
    with open(FIXTURE, "rb") as fh:
        frames = list(iter_pcap_frames(fh))
    assert len(frames) == 207


# The 802.11 bytes iter_pcap_frames yielded for flock_ch1.pcap before it read the linktype: each
# frame's length (little-endian uint32) then its bytes, hashed in order. The tcpdump path must
# keep yielding exactly these bytes.
FIXTURE_FRAMES_SHA256 = "1eb96fe4a9d21fd48fba419d8c67c578380ab1148915998f40ff0afc984d66de"


def test_iter_pcap_frames_keeps_the_real_radiotap_capture_byte_for_byte():
    digest = hashlib.sha256()
    with open(FIXTURE, "rb") as fh:
        for frame in iter_pcap_frames(fh):
            digest.update(struct.pack("<I", len(frame)) + frame)
    assert digest.hexdigest() == FIXTURE_FRAMES_SHA256


def raw_pcap_stream(frames, linktype):
    """A pcap byte stream carrying each frame as-is under the given linktype (no radiotap)."""
    out = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 4096, linktype)
    for frame in frames:
        out += struct.pack("<IIII", 0, 0, len(frame), len(frame)) + frame
    return out


def test_iter_pcap_frames_passes_linktype_105_802_11_through_untouched():
    # LINKTYPE_IEEE802_11 is bare 802.11, the linktype ESP32 Marauder writes: there is no
    # radiotap header to strip, so the record is the frame.
    probe = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x01", BROADCAST, FLOCK_IES)
    beacon = mgmt_frame(8, BROADCAST, NON_FLOCK + b"\x00\x00\x09", NON_FLOCK + b"\x00\x00\x09")
    frames = list(iter_pcap_frames(io.BytesIO(raw_pcap_stream([probe, beacon], 105))))
    assert frames == [probe, beacon]


def test_iter_pcap_frames_refuses_a_linktype_it_cannot_read():
    # Ethernet (linktype 1) is not 802.11. Guessing at its layout would turn bytes into made-up
    # addresses, so the reader says so loudly instead.
    stream = io.BytesIO(raw_pcap_stream([b"\x00" * 60], 1))
    with pytest.raises(ValueError, match="linktype 1"):
        list(iter_pcap_frames(stream))


def test_every_frame_of_the_capture_fixture_says_it_ends_in_its_fcs():
    # wlan1's radiotap names TSFT and Flags behind two present words, and its Flags carry 0x10.
    with open(FIXTURE, "rb") as fh:
        says = [has_fcs for _, has_fcs in iter_pcap_captures(fh)]
    assert len(says) == 207 and all(says)


def test_parses_real_probe_requests_with_sane_macs_and_signatures():
    with open(FIXTURE, "rb") as fh:
        infos = [parse_management_frame(f, has_fcs=has_fcs)
                 for f, has_fcs in iter_pcap_captures(fh)]
    probes = [i for i in infos if i is not None and i.is_probe_request]
    assert len(probes) == 63

    srcs = {p.src for p in probes}
    assert "02:00:00:00:04:20" in srcs          # made up, locally administered
    assert "02:00:00:00:07:10" in srcs
    assert all(MAC_RE.match(s) for s in srcs)

    # The example_one probes all carry supported-rates (1) + extended rates (50) and nothing else.
    # Each frame here ends in its 4-byte FCS, which the walk over the whole frame reads as one more
    # tag, usually a truncated one, so those fingerprints share only the 1,50 prefix; without the
    # FCS each one is exactly 1,50.
    example_one = [p for p in probes if p.ssid == "example_one"]
    assert len(example_one) > 40
    assert all(p.ie_signature.startswith("1,50") for p in example_one)
    assert all(p.ie_signature_without_fcs == "1,50" for p in example_one)


def test_parse_rejects_short_and_non_management_frames():
    assert parse_management_frame(b"") is None
    assert parse_management_frame(b"\x40\x00") is None            # too short for a mgmt header
    control = bytes([0x74, 0x00]) + b"\x00" * 22                   # ftype 1 = control, not mgmt
    assert parse_management_frame(control) is None


def test_parse_beacon_is_management_but_not_a_probe_request():
    beacon = mgmt_frame(8, BROADCAST, NON_FLOCK + b"\x00\x00\x01", NON_FLOCK + b"\x00\x00\x01")
    info = parse_management_frame(beacon)
    assert info is not None
    assert info.ftype == 0 and info.subtype == 8
    assert info.is_probe_request is False
    assert info.ie_signature == ""


def test_parse_records_the_three_addresses():
    frame = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x01", BROADCAST, FLOCK_IES)
    info = parse_management_frame(frame)
    assert info.src == "b4:1e:52:00:00:01"
    assert info.dst == "ff:ff:ff:ff:ff:ff"
    assert info.bssid == "ff:ff:ff:ff:ff:ff"
    assert info.ssid_wildcard is True
    assert info.ie_signature == FLOCK_IE_SIGNATURE


# --------------------------------------------------------------------------- signature tables


def test_signature_tables_are_the_upstream_set():
    assert len(FLOCK_OUIS) == 34
    assert "b4:1e:52" in FLOCK_OUIS
    assert "00:03:7f" in FLOCK_OUIS            # Qualcomm Atheros, firmware-extracted
    assert "82:6b:f2" in FLOCK_OUIS            # locally-administered, still in the set
    assert FLOCK_IE_SIGNATURE == "2,12,127,221:506f9a16030103,45,191,221:0050f208000000"


# --------------------------------------------------------------------------- match tiers


def test_tier4_probe_from_flock_oui_with_matching_ie_fingerprint():
    frame = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x01", BROADCAST, FLOCK_IES)
    hit = match_signatures(parse_management_frame(frame))
    assert hit is not None
    assert hit.tier == 4
    assert hit.mac == "b4:1e:52:00:00:01"
    assert hit.oui == "b4:1e:52"


def test_tier3_wildcard_probe_from_flock_oui_without_the_fingerprint():
    frame = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x02", BROADCAST, b"\x00\x00")
    hit = match_signatures(parse_management_frame(frame))
    assert hit.tier == 3
    assert hit.oui == "b4:1e:52"


def test_tier2_flock_oui_as_transmitter_on_a_non_probe_frame():
    beacon = mgmt_frame(8, BROADCAST, FLOCK_OUI + b"\x00\x00\x03", FLOCK_OUI + b"\x00\x00\x03")
    hit = match_signatures(parse_management_frame(beacon))
    assert hit.tier == 2
    assert hit.mac == "b4:1e:52:00:00:03"


def test_tier2_matches_locally_administered_flock_mac():
    beacon = mgmt_frame(8, BROADCAST, FLOCK_OUI_LOCALLY_ADMIN + b"\x00\x00\x01",
                        FLOCK_OUI_LOCALLY_ADMIN + b"\x00\x00\x01")
    hit = match_signatures(parse_management_frame(beacon))
    assert hit is not None
    assert hit.oui == "82:6b:f2"               # LA bit set, not skipped


def test_tier1_flock_oui_as_unicast_receiver():
    # A mgmt frame addressed TO a Flock OUI (addr1), transmitted by something else.
    frame = mgmt_frame(5, FLOCK_OUI + b"\x00\x00\x04", NON_FLOCK + b"\x00\x00\x09",
                       NON_FLOCK + b"\x00\x00\x09")
    hit = match_signatures(parse_management_frame(frame))
    assert hit.tier == 1
    assert hit.mac == "b4:1e:52:00:00:04"


def test_tier1_flock_oui_as_bssid():
    frame = mgmt_frame(8, BROADCAST, NON_FLOCK + b"\x00\x00\x09", FLOCK_OUI + b"\x00\x00\x05")
    hit = match_signatures(parse_management_frame(frame))
    assert hit.tier == 1
    assert hit.mac == "b4:1e:52:00:00:05"


def test_tier0_ssid_keyword_only_when_enabled():
    frame = mgmt_frame(4, BROADCAST, NON_FLOCK + b"\x00\x00\x09", BROADCAST,
                       ssid_ies("Flock Safety"))
    info = parse_management_frame(frame)
    assert match_signatures(info) is None                       # off by default
    hit = match_signatures(info, ssid_match_enabled=True)
    assert hit.tier == 0
    assert hit.ssid == "Flock Safety"


def test_highest_tier_wins_when_several_rules_fire():
    # A Flock-OUI probe carrying the fingerprint also qualifies for tier 2 (OUI transmitter);
    # the matcher must report the fingerprint, not the OUI.
    frame = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x06", BROADCAST, FLOCK_IES)
    assert match_signatures(parse_management_frame(frame)).tier == 4


def test_no_probe_in_the_capture_fixture_is_a_flock_hit():
    with open(FIXTURE, "rb") as fh:
        infos = [parse_management_frame(f) for f in iter_pcap_frames(fh)]
    probes = [i for i in infos if i is not None and i.is_probe_request]
    assert probes
    assert all(match_signatures(p, ssid_match_enabled=True) is None for p in probes)


# --------------------------------------------------------------------------- trailing FCS

# A capture can keep each frame's 4-byte frame check sequence (FCS) after its last element, and
# radiotap then sets the FCS bit (0x10) of its Flags field. A capture off wlan1 keeps it on every
# frame (flock_ch1.pcap: all 207); Marauder drops it before it writes a frame. A frame that radiotap
# says ends in its FCS is fingerprinted whole and again without its last 4 bytes, the two tries
# upstream makes on every frame (flock-you @6704f01 main.cpp, fyProbeBodyFlockIeSigPrimary)
# because its ESP32 radio always hands it the FCS. Any other frame is fingerprinted whole only. The
# device parts of the addresses are made up and locally administered, behind the prefix 82:6b:f2.
RADIOTAP_FLAGS_FIELD = 0x00000002      # radiotap present bit 1: the one-octet Flags field
RADIOTAP_F_FCS = 0x10                  # Flags: the frame ends in its FCS
FLOCK_CAMERA = FLOCK_OUI_LOCALLY_ADMIN + b"\x00\x00\xfc"
OTHER_CAMERA = FLOCK_OUI_LOCALLY_ADMIN + b"\x00\x00\xfb"
EXTRA_ELEMENT = bytes.fromhex("3b025151")    # one more 4-byte element: operating classes 81, 81
TIER4 = (4, "probe-request IE fingerprint")
TIER3 = (3, "wildcard probe from Flock OUI")


def probe_request(transmitter, ies, seq=0):
    """A broadcast probe request from `transmitter`, sequence number `seq`, carrying `ies`."""
    return (bytes([0x40, 0x00]) + b"\x00\x00" + BROADCAST + transmitter + BROADCAST
            + struct.pack("<H", seq << 4) + ies)


def fcs(dot11):
    """A frame's FCS as a capture keeps it: the CRC-32 of its bytes, low octet first."""
    return struct.pack("<I", zlib.crc32(dot11))


def flock_probe_whose_fcs(shape, ies=FLOCK_IES):
    """A probe from FLOCK_CAMERA carrying `ies`, by default the fingerprint, with the first
    sequence number whose FCS has this shape."""
    for seq in range(4096):
        probe = probe_request(FLOCK_CAMERA, ies, seq)
        if shape(fcs(probe)):
            return probe
    raise AssertionError("no sequence number gives an FCS of that shape")


def radiotap_record(dot11, flags):
    """`dot11` behind a radiotap header that carries only the Flags field."""
    return struct.pack("<BBHIB", 0, 0, 9, RADIOTAP_FLAGS_FIELD, flags) + dot11


def radiotap_pcap(*records):
    """A linktype-127 pcap stream of records already behind their radiotap headers."""
    out = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 512, 127)
    for rec in records:
        out += struct.pack("<IIII", 0, 0, len(rec), len(rec)) + rec
    return out


def tcpdump_hits(*records):
    """What the tcpdump path makes of radiotap records: the match for each frame, in order."""
    return [match_signatures(parse_management_frame(f, has_fcs=has_fcs))
            for f, has_fcs in iter_pcap_captures(io.BytesIO(radiotap_pcap(*records)))]


def test_tier4_matches_with_and_without_a_trailing_fcs():
    # The usual FCS reads as a tag whose declared length runs past the end of the frame.
    probe = flock_probe_whose_fcs(lambda f: f[0] != 0 and f[1] > 2)
    hits = tcpdump_hits(radiotap_record(probe + fcs(probe), RADIOTAP_F_FCS),
                        radiotap_record(probe, 0))
    assert [(h.tier, h.rule) for h in hits] == [TIER4, TIER4]


def test_tier4_matches_when_the_fcs_reads_as_a_whole_element():
    # About one frame in 256 ends in an FCS whose second octet is 2: a complete two-byte element.
    probe = flock_probe_whose_fcs(lambda f: f[0] != 0 and f[1] == 2)
    (hit,) = tcpdump_hits(radiotap_record(probe + fcs(probe), RADIOTAP_F_FCS))
    assert (hit.tier, hit.rule) == TIER4


def test_a_last_four_byte_element_stays_in_the_fingerprint_with_or_without_an_fcs():
    # A real element after the fingerprint is never taken for an FCS: with the flag clear nothing
    # is stripped, and with it set only the FCS is. Either way the fingerprint is not exact.
    probe = probe_request(FLOCK_CAMERA, FLOCK_IES + EXTRA_ELEMENT)
    hits = tcpdump_hits(radiotap_record(probe, 0),
                        radiotap_record(probe + fcs(probe), RADIOTAP_F_FCS))
    assert [(h.tier, h.rule) for h in hits] == [TIER3, TIER3]


def test_a_record_the_snaplen_cut_short_has_lost_its_fcs():
    # The flag still says the frame ends in its FCS, but tcpdump kept only the bytes before it: the
    # record ends in the frame's own last element, and nothing may be stripped.
    probe = probe_request(FLOCK_CAMERA, FLOCK_IES + EXTRA_ELEMENT)
    whole = radiotap_record(probe + fcs(probe), RADIOTAP_F_FCS)
    kept = whole[:-4]
    stream = (struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, len(kept), 127)
              + struct.pack("<IIII", 0, 0, len(kept), len(whole)) + kept)
    [(dot11, has_fcs)] = iter_pcap_captures(io.BytesIO(stream))
    hit = match_signatures(parse_management_frame(dot11, has_fcs=has_fcs))
    assert (has_fcs, (hit.tier, hit.rule)) == (False, TIER3)


def test_a_tag_running_past_the_end_gives_its_id_and_ends_the_walk():
    # On wlan1 such a tag is usually the kept FCS. Here tag 127 declares 8 bytes and 3 are left;
    # they would read as one more element (tag 45, length 1) if the walk went on past it.
    ies = bytes.fromhex("0000" "020100" "0c0100" "7f08" "2d0100")
    info = parse_management_frame(probe_request(FLOCK_CAMERA, ies))
    assert info.ie_signature == "2,12,127"


def test_a_wildcard_probe_stays_tier3_when_its_fcs_starts_with_zero():
    # An FCS whose first octet is 0 reads as one more SSID element, about one frame in 256, and
    # with a second octet above 0 it would name a directed probe. The first SSID element is the
    # probe's, as upstream reads it.
    ies = bytes.fromhex("0000" "010482848b96")     # wildcard SSID, supported rates; no fingerprint
    probe = flock_probe_whose_fcs(lambda f: f[0] == 0 and f[1] > 0, ies)
    (hit,) = tcpdump_hits(radiotap_record(probe + fcs(probe), RADIOTAP_F_FCS))
    assert (hit.tier, hit.rule, hit.ssid) == (3, "wildcard probe from Flock OUI", "")


# --------------------------------------------------------------------------- roster


def test_roster_accumulates_count_and_keeps_the_best_tier():
    roster = FlockRoster()
    low = match_signatures(parse_management_frame(
        mgmt_frame(8, BROADCAST, FLOCK_OUI + b"\x00\x00\x07", FLOCK_OUI + b"\x00\x00\x07")))
    high = match_signatures(parse_management_frame(
        mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x07", BROADCAST, FLOCK_IES)))
    roster.absorb(low, now=1.0)
    roster.absorb(high, now=2.0)
    roster.absorb(low, now=3.0)
    rows = roster.detections()
    assert len(rows) == 1
    assert rows[0].count == 3
    assert rows[0].tier == 4                 # best tier sticks
    assert rows[0].last_seen == 3.0


def test_roster_orders_newest_first():
    roster = FlockRoster()
    a = match_signatures(parse_management_frame(
        mgmt_frame(8, BROADCAST, FLOCK_OUI + b"\x00\x00\x0a", FLOCK_OUI + b"\x00\x00\x0a")))
    b = match_signatures(parse_management_frame(
        mgmt_frame(8, BROADCAST, FLOCK_OUI + b"\x00\x00\x0b", FLOCK_OUI + b"\x00\x00\x0b")))
    roster.absorb(a, now=1.0)
    roster.absorb(b, now=2.0)
    rows = roster.detections()
    assert [r.mac for r in rows] == ["b4:1e:52:00:00:0b", "b4:1e:52:00:00:0a"]


# --------------------------------------------------------------------------- detector


class FakeProcess:
    """Stands in for the tcpdump process at the boundary (its stdout is a binary pcap stream)."""

    def __init__(self, data):
        self.stdout = io.BytesIO(data)
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _detector(data, **kw):
    kw.setdefault("radio_present", lambda: True)
    kw.setdefault("which", lambda name: "/usr/bin/" + name)
    return FlockDetector(spawn=lambda interface: FakeProcess(data), **kw)


def test_detector_populates_the_roster_from_a_pcap_stream():
    flock_probe = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x08", BROADCAST, FLOCK_IES)
    dec = _detector(pcap_stream([flock_probe, flock_probe]))
    dec.start()
    dec.drain()
    rows = dec.detections()
    assert len(rows) == 1
    assert rows[0]["mac"] == "b4:1e:52:00:00:08"
    assert rows[0]["tier"] == 4
    assert rows[0]["count"] == 2
    assert dec.error is None


def test_the_tcpdump_source_strips_only_an_fcs_that_radiotap_flags():
    # One camera's probe ends in its FCS, flagged; the other's ends in a real 4-byte element, with
    # no flag. Only the first may be fingerprinted without its last 4 bytes.
    flagged = flock_probe_whose_fcs(lambda f: f[0] != 0 and f[1] > 2)
    unflagged = probe_request(OTHER_CAMERA, FLOCK_IES + EXTRA_ELEMENT)
    dec = _detector(radiotap_pcap(radiotap_record(flagged + fcs(flagged), RADIOTAP_F_FCS),
                                  radiotap_record(unflagged, 0)))
    dec.start()
    dec.drain()
    assert {r["mac"]: r["tier"] for r in dec.detections()} == {
        "82:6b:f2:00:00:fc": 4, "82:6b:f2:00:00:fb": 3}


def test_detector_reports_nothing_on_the_real_negative_capture():
    data = FIXTURE.read_bytes()
    dec = _detector(data)
    dec.start()
    dec.drain()
    assert dec.detections() == []              # a true-negative capture must stay empty
    assert dec.error is None


def test_detector_stop_tears_down_the_capture():
    proc = FakeProcess(FIXTURE.read_bytes())
    dec = FlockDetector(spawn=lambda interface: proc, radio_present=lambda: True,
                        which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.running is True                 # the fake process never exits on its own
    dec.stop()
    assert dec.running is False
    assert proc.terminated is True


def test_detector_refuses_when_tcpdump_is_missing():
    dec = _detector(b"", which=lambda name: None if name == "tcpdump" else "/usr/bin/" + name)
    with pytest.raises(FlockUnavailable):
        dec.start()


def test_detector_refuses_without_the_monitor_interface():
    dec = _detector(b"", radio_present=lambda: False)
    with pytest.raises(FlockUnavailable):
        dec.start()


def test_a_reader_error_is_recorded_not_swallowed():
    class RaisingProcess(FakeProcess):
        def __init__(self):
            super().__init__(b"")

            class Boom:
                def read(self, _n=-1):
                    raise OSError("capture pipe died")

            self.stdout = Boom()

    dec = FlockDetector(spawn=lambda interface: RaisingProcess(),
                        radio_present=lambda: True, which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "capture pipe died"


# --------------------------------------------------------------------------- Marauder serial

# CONSTRUCTED FROM MARAUDER'S SOURCE, NOT CAPTURED. No devboard has streamed over the reference
# Recon's bridge yet, so this wraps the real probe requests of flock_ch1.pcap byte for byte the
# way ESP32 Marauder v1.20.0 says it emits them over serial; a real on-air capture through the
# Flipper's bridge replaces it at HIL. Each piece cites what it reproduces (esp32_marauder/ at
# v1.20.0):
# - the console echoes a command as "#<cmd>", prints its reply, then the "> " prompt
#   (CommandLine.cpp:270, :87); println ends a line with CRLF;
# - a pcap opens with a 24-byte global header, linktype 105, snaplen 4096 on the Flipper build
#   (PcapHeader.cpp:20-29, configs.h:3373-3375 with HAS_PSRAM from :529);
# - a record is a 16-byte header {secs, usecs, len, len} from micros(), then the frame
#   (Buffer.cpp:122-139);
# - every main-loop pass sends the buffer as one write between "[BUF/BEGIN]" and "[BUF/CLOSE]"
#   (Buffer.cpp:228-267, esp32_marauder.ino:552), so the first pass carries the header alone;
# - sniffprobe prints a console line for each probe request before buffering it
#   (WiFiScan.cpp:9092-9144), with the client MAC in lowercase (WiFiScan.cpp:7416-7417).
MARAUDER_BEGIN = b"[BUF/BEGIN]"
MARAUDER_CLOSE = b"[BUF/CLOSE]"
MARAUDER_SNAPLEN = 4096


def marauder_console(command, *replies):
    """What Marauder's console prints for one command: its echo, its reply lines, the prompt."""
    return "".join(line + "\r\n" for line in (f"#{command}", *replies)).encode() + b"> "


MARAUDER_STARTUP = (
    marauder_console("stopscan", "Stopping WiFi tran/recv")
    + marauder_console("channel -s 1", "1", "1")
    + marauder_console("sniffprobe -serial", "StartingProbe sniff. Stop with stopscan"))


def marauder_header(linktype=105):
    return struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, MARAUDER_SNAPLEN, linktype)


def marauder_record(frame, micros):
    secs = micros // 1_000_000
    return struct.pack("<IIII", secs, micros - secs * 1_000_000, len(frame), len(frame)) + frame


def marauder_probe_line(frame, rssi=-61, channel=1):
    """sniffprobe's console line for one probe request: RSSI, channel, client, the SSID's bytes."""
    client = ":".join(f"{b:02x}" for b in frame[10:16])
    ssid = frame[26:26 + frame[25]]
    return f"{rssi} Ch: {channel} Client: {client} Requesting: ".encode() + ssid + b"\r\n"


def marauder_chunk(body):
    return MARAUDER_BEGIN + body + MARAUDER_CLOSE


def marauder_stream(frames, per_chunk=5, startup=MARAUDER_STARTUP):
    """A sniffprobe session as the serial line carries it: the console, then the pcap in chunks."""
    out = startup + marauder_chunk(marauder_header())
    micros = 1_250_000
    for start in range(0, len(frames), per_chunk):
        body = b""
        for frame in frames[start:start + per_chunk]:
            out += marauder_probe_line(frame)
            micros += 37_000
            body += marauder_record(frame, micros)
        out += marauder_chunk(body)
    return out


def fixture_probes():
    """flock_ch1.pcap's probe requests: the frames sniffprobe keeps (first octet 0x40)."""
    with open(FIXTURE, "rb") as fh:
        return [f for f in iter_pcap_frames(fh) if f[0] == 0x40]


class Trickle:
    """A serial line that hands over one byte per read, so every marker and record is split."""

    def __init__(self, data):
        self._data = data
        self._at = 0

    def read(self, _n=-1):
        chunk = self._data[self._at:self._at + 1]
        self._at += len(chunk)
        return chunk


def test_marauder_reader_yields_every_probe_the_session_carried():
    probes = fixture_probes()
    assert len(probes) == 63
    assert list(iter_marauder_frames(io.BytesIO(marauder_stream(probes)))) == probes


def test_marauder_reader_survives_reads_that_split_markers_and_records():
    probes = fixture_probes()
    assert list(iter_marauder_frames(Trickle(marauder_stream(probes)))) == probes


def test_marauder_frames_take_the_same_detector_path_as_tcpdumps():
    # Our parser and matcher judge them, never Marauder's own verdicts: the same 63 real probes,
    # the same honest true negative.
    stream = io.BytesIO(marauder_stream(fixture_probes()))
    infos = [parse_management_frame(f) for f in iter_marauder_frames(stream)]
    assert len(infos) == 63 and all(i.is_probe_request for i in infos)
    assert all(match_signatures(i, ssid_match_enabled=True) is None for i in infos)


def test_marauder_reader_reads_records_by_length_not_by_marker():
    # An SSID is any 32 bytes, so a probe can carry the close marker. Reading by the record's
    # length keeps it inside its frame instead of ending the chunk there.
    sneaky = mgmt_frame(4, BROADCAST, NON_FLOCK + b"\x00\x00\x0c", BROADCAST,
                        ssid_ies("[BUF/CLOSE]"))
    flock = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x0d", BROADCAST, FLOCK_IES)
    stream = io.BytesIO(marauder_stream([sneaky, flock]))
    assert list(iter_marauder_frames(stream)) == [sneaky, flock]


def test_marauder_reader_drops_a_false_start_printed_on_the_console():
    # sniffprobe prints the SSID a client asked for, raw. One named "[BUF/BEGIN]" puts a false
    # chunk start in the console text; what follows is no pcap record, so the reader goes back to
    # looking for the next real chunk.
    sneaky = mgmt_frame(4, BROADCAST, NON_FLOCK + b"\x00\x00\x0e", BROADCAST,
                        ssid_ies("[BUF/BEGIN]"))
    flock = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x0f", BROADCAST, FLOCK_IES)
    data = marauder_stream([sneaky, flock], per_chunk=1)
    assert MARAUDER_BEGIN in marauder_probe_line(sneaky)
    assert list(iter_marauder_frames(io.BytesIO(data))) == [sneaky, flock]


def test_marauder_reader_skips_chunks_heard_before_a_global_header():
    # Joined mid-sniff (one left running), the chunks hold records of a pcap whose header went by
    # unheard, so their linktype is unknown: they are skipped until a pcap opens.
    stale = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x10", BROADCAST, FLOCK_IES)
    fresh = fixture_probes()[0]
    data = (marauder_probe_line(stale) + marauder_chunk(marauder_record(stale, 9_000_000))
            + marauder_stream([fresh]))
    assert list(iter_marauder_frames(io.BytesIO(data))) == [fresh]


def test_marauder_reader_follows_a_pcap_reopened_mid_stream():
    probes = fixture_probes()
    data = marauder_stream(probes[:3]) + marauder_stream(probes[3:6])
    assert list(iter_marauder_frames(io.BytesIO(data))) == probes[:6]


def test_marauder_reader_refuses_a_linktype_it_cannot_read():
    data = MARAUDER_STARTUP + marauder_chunk(marauder_header(linktype=1))
    with pytest.raises(ValueError, match="linktype 1"):
        list(iter_marauder_frames(io.BytesIO(data)))


# --------------------------------------------------------------------------- Marauder source


class Bridge:
    """A real tty pair standing in for the Flipper's USB-UART bridge port.

    The detector opens the slave end exactly as it opens the bridge's CDC port (real termios, real
    select); the test plays the devboard on the master end. The double sits only at the hardware
    boundary: everything between the port and the roster is the product's own code.
    """

    def __init__(self):
        self.master, self._slave = os.openpty()
        self.path = os.ttyname(self._slave)

    def heard(self, quiet=0.3):
        """What the detector has typed so far, read until the line goes quiet."""
        out = b""
        while select.select([self.master], [], [], quiet)[0]:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                break
            if not data:
                break
            out += data
        return out

    def say(self, data, timeout=5.0):
        """Send bytes as the devboard would, never blocking past the timeout."""
        deadline = time.monotonic() + timeout
        while data:
            if not select.select([], [self.master], [], max(0.0, deadline - time.monotonic()))[1]:
                raise AssertionError("the detector stopped reading the port")
            data = data[os.write(self.master, data):]

    def hang_up(self):
        """The bridge goes away (the Flipper unplugged, or out of USB-UART Bridge)."""
        os.close(self.master)
        self.master = None

    def close(self):
        for fd in (self.master, self._slave):
            if fd is not None:
                os.close(fd)


@pytest.fixture
def bridge():
    port = Bridge()
    yield port
    port.close()


def wait_for(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.02)


def test_marauder_source_types_stop_channel_and_sniff_then_stopscan_on_stop(bridge):
    dec = MarauderFlockDetector(tty=bridge.path)
    dec.start()
    try:
        assert bridge.heard() == b"stopscan\nchannel -s 1\nsniffprobe -serial\n"
        assert dec.live_command() == "sniffprobe -serial"     # the command strip's real line
        assert dec.interface == bridge.path
    finally:
        dec.stop()
    assert bridge.heard() == b"stopscan\n"
    assert dec.running is False
    assert dec.live_command() is None


def test_marauder_source_goes_live_once_marauder_answers_and_detects_from_its_frames(
        bridge, monkeypatch):
    monkeypatch.setattr(childproc, "SETTLE_S", 0.0)
    flock = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x11", BROADCAST, FLOCK_IES)
    dec = MarauderFlockDetector(tty=bridge.path)
    dec.start()
    try:
        bridge.heard()
        assert dec.watch.settling() is True        # typed to, but no answer yet: starting
        bridge.say(marauder_stream(fixture_probes()[:20] + [flock]))
        wait_for(lambda: dec.detections())
        rows = dec.detections()
        assert [(r["mac"], r["tier"]) for r in rows] == [("b4:1e:52:00:00:11", 4)]
        assert dec.watch.settling() is False       # the pcap is flowing: live
        assert dec.running is True and dec.error is None
    finally:
        dec.stop()


def test_the_marauder_source_never_strips_a_frames_last_four_bytes(bridge):
    # Marauder drops each management frame's FCS before it writes the frame (v1.20.0
    # WiFiScan.cpp:9013), so its last 4 bytes are always the frame's own: the fingerprint followed
    # by one more 4-byte element stays tier 3.
    probe = probe_request(FLOCK_CAMERA, FLOCK_IES + EXTRA_ELEMENT)
    dec = MarauderFlockDetector(tty=bridge.path)
    dec.start()
    try:
        bridge.heard()
        bridge.say(marauder_stream([probe]))
        wait_for(lambda: dec.detections())
        assert [(r["mac"], r["tier"]) for r in dec.detections()] == [("82:6b:f2:00:00:fc", 3)]
    finally:
        dec.stop()


def test_marauder_source_without_the_bridge_port_has_no_monitor_radio(tmp_path):
    dec = MarauderFlockDetector(tty=str(tmp_path / "ttyACM1"))
    with pytest.raises(FlockUnavailable, match="no monitor radio") as refused:
        dec.start()
    assert "GPIO → USB-UART Bridge" in str(refused.value)
    assert dec.running is False
    assert dec.watch.stopped is False             # never ran: no radio, not a stopped one


def test_marauder_source_looks_for_the_bridge_port_by_id():
    # The bridge on USB channel 1 is the Flipper's second CDC port, USB interface 2.
    asked = []
    dec = MarauderFlockDetector(glob=lambda pattern: asked.append(pattern) or [])
    with pytest.raises(FlockUnavailable, match="no monitor radio"):
        dec.start()
    assert asked == [MARAUDER_TTY_GLOB] == ["/dev/serial/by-id/*Flipper*-if02"]


def test_marauder_source_uses_the_one_bridge_port_it_finds(bridge):
    dec = MarauderFlockDetector(glob=lambda pattern: [bridge.path])
    dec.start()
    try:
        assert bridge.heard().endswith(b"sniffprobe -serial\n")
        assert dec.interface == bridge.path
    finally:
        dec.stop()


def test_marauder_source_refuses_to_guess_between_two_bridges():
    two = ["/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_Aaa_flip_Aaa-if02",
           "/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_Bbb_flip_Bbb-if02"]
    dec = MarauderFlockDetector(glob=lambda pattern: two)
    with pytest.raises(FlockUnavailable, match="more than one"):
        dec.start()


def test_marauder_source_refuses_a_path_that_is_no_serial_port(tmp_path):
    plain = tmp_path / "ttyACM1"
    plain.write_bytes(b"")
    dec = MarauderFlockDetector(tty=str(plain))
    with pytest.raises(FlockUnavailable, match="cannot open"):
        dec.start()


def test_marauder_source_nobody_answers_on_is_stopped_with_that_reason(bridge):
    dec = MarauderFlockDetector(tty=bridge.path, answer_timeout=0.3)
    dec.start()
    dec.drain()
    with pytest.raises(FlockUnavailable, match="no answer from Marauder") as stopped:
        dec.start()
    assert bridge.path in str(stopped.value)
    assert dec.watch.stopped is True
    assert dec.watch.reason == str(stopped.value)  # its own words, never an invented exit status


def test_marauder_source_that_loses_its_link_is_stopped_with_that_reason(bridge):
    dec = MarauderFlockDetector(tty=bridge.path)
    dec.start()
    bridge.say(marauder_stream([]))
    wait_for(lambda: dec._proc.answered)
    bridge.hang_up()
    dec.drain()
    assert dec.running is False
    assert dec.watch.stopped is True
    # A hung-up tty reads as end of file or as EIO depending on timing; either is said as such.
    assert re.fullmatch(rf"the serial link to Marauder on {re.escape(bridge.path)} closed "
                        r"\((end of file|Input/output error)\)", dec.watch.reason)
    # The port went with the bridge, so the next poll says the radio is gone, not merely stopped.
    with pytest.raises(FlockUnavailable, match="no monitor radio"):
        dec.start()


def test_marauder_source_whose_devboard_restarts_is_stopped_not_left_live(bridge):
    # A devboard that browns out and reboots comes back idle: its sniff is gone, and a quiet link
    # would otherwise read LIVE forever. Marauder's boot prints the ESP-IDF version first
    # (esp32_marauder.ino:383); the rest of this boot text is illustrative.
    dec = MarauderFlockDetector(tty=bridge.path)
    dec.start()
    bridge.say(marauder_stream([]))
    wait_for(lambda: dec._proc.answered)
    bridge.say(b"ESP-ROM:esp32s2-rc4-20191025\r\nESP-IDF version is: v4.4.4\r\n")
    dec.drain()
    assert dec.running is False
    assert dec.watch.reason.startswith("Marauder restarted on the devboard")


def test_marauder_retires_after_restart_without_another_start(bridge):
    dec = MarauderFlockDetector(tty=bridge.path)
    dec.start()
    try:
        link, run = dec._proc, dec._runs.current
        bridge.say(marauder_stream([]))
        wait_for(lambda: link.answered)
        bridge.say(b"ESP-IDF version is: v4.4.4\r\n")
        wait_for(lambda: link.poll() == 1)
        wait_for(lambda: dec._runs.current != run)
        assert dec.watch.reason == link.end_reason
        assert not dec.running
    finally:
        dec.stop()


@pytest.mark.parametrize("ending", ["coalesced", "split", "marker-alone", "first-chunk"])
def test_marauder_reboot_delivers_only_capture_before_the_marker(monkeypatch, ending):
    # A pipe fixes the read boundaries. Detection and exit reporting still use the real link
    # and detector, and a valid frame after the marker would be visible if it leaked through.
    first, last, after = (
        mgmt_frame(4, BROADCAST, FLOCK_OUI + bytes((0, 0, index)), BROADCAST, FLOCK_IES)
        for index in (1, 2, 3)
    )
    boot = flock_module.MARAUDER_BOOT
    initial = marauder_stream([first], startup=boot + b"v4.4.4\r\n")
    final = marauder_chunk(marauder_record(last, 2_000_000))
    trailer = b"v4.4.4\r\n" + marauder_chunk(marauder_record(after, 3_000_000))
    reader, writer = os.pipe()
    link = MarauderLink(reader, "bridge")
    dec = MarauderFlockDetector(glob=lambda _pattern: [link.path])
    monkeypatch.setattr(dec, "_open_capture", lambda: link)
    reported = []
    delivered = []
    read = link.read

    def record_read(size):
        data = read(size)
        delivered.append(data)
        return data

    monkeypatch.setattr(link, "read", record_read)
    dec.on_reception = lambda item: reported.append((item["mac"], link.poll()))
    expected_macs = ["b4:1e:52:00:00:01"]
    before_boot = final if ending in ("coalesced", "split") else b""
    if before_boot:
        expected_macs.append("b4:1e:52:00:00:02")
    expected_data = initial + before_boot
    split_at = len(boot) // 2 if ending == "split" else 0
    with os.fdopen(writer, "wb", buffering=0) as serial:
        try:
            dec.start()
            if ending == "first-chunk":
                before_boot = initial
            else:
                serial.write(initial)
                wait_for(lambda: len(reported) == 1)
            if split_at:
                serial.write(before_boot + boot[:split_at])
                wait_for(lambda: len(reported) == 2)
                before_boot = b""
            serial.write(before_boot + boot[split_at:] + trailer)
            dec.drain(timeout=1.0)
            assert reported == [(mac, None) for mac in expected_macs]
            assert {row["mac"] for row in dec.detections()} == set(expected_macs)
            assert b"".join(delivered) in (expected_data, expected_data + boot[:split_at])
            reason = ("Marauder restarted on the devboard (its boot text arrived), "
                      "so its sniff stopped")
            assert not dec.running and link.wait(timeout=0) == 1
            assert link.end_reason == dec.watch.reason == reason
        finally:
            dec.stop()
            link.kill()


def test_marauder_stream_it_cannot_read_stops_the_sniff_and_says_why(bridge):
    dec = MarauderFlockDetector(tty=bridge.path)
    dec.start()
    bridge.heard()
    bridge.say(MARAUDER_STARTUP + marauder_chunk(marauder_header(linktype=1)))
    dec.drain()
    with pytest.raises(FlockUnavailable, match="linktype 1"):
        dec.start()
    assert bridge.heard() == b"stopscan\n"       # Marauder is not left streaming to nobody


def test_a_marauder_link_keeps_the_stop_contract_a_process_keeps(bridge):
    # stop_process asks (terminate), waits, then insists (kill): the link answers all three the
    # way a child does, so the watch and the stop path need no special case for it.
    link = MarauderLink(_open_marauder_port(bridge.path), bridge.path)
    assert link.poll() is None
    with pytest.raises(subprocess.TimeoutExpired):
        link.wait(timeout=0.01)
    link.kill()                                   # insisting: closed without typing stopscan
    assert link.wait(timeout=0) == 0 and link.end_reason is None
    assert bridge.heard() == b""
    with pytest.raises(OSError):
        link.send("sniffprobe -serial")
    assert link.read() == b""


def test_the_site_picks_the_flock_radio_from_the_environment():
    assert type(configured_flock_detector({})) is FlockDetector
    assert type(configured_flock_detector({"SIGINT101_FLOCK_SOURCE": "tcpdump"})) is FlockDetector
    by_id = configured_flock_detector({"SIGINT101_FLOCK_SOURCE": "marauder"})
    assert isinstance(by_id, MarauderFlockDetector)
    assert by_id.interface == MARAUDER_TTY_GLOB
    pinned = configured_flock_detector({"SIGINT101_FLOCK_SOURCE": " Marauder ",
                                        "SIGINT101_MARAUDER_TTY": "/dev/ttyACM1"})
    assert isinstance(pinned, MarauderFlockDetector)
    assert pinned.interface == "/dev/ttyACM1"


def test_the_console_builds_the_marauder_source_when_the_site_asks(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGINT101_FLOCK_SOURCE", "marauder")
    monkeypatch.setenv("SIGINT101_MARAUDER_TTY", str(tmp_path / "ttyACM1"))
    with _client(tmp_path) as client:
        resp = client.get("/api/flock")
    assert resp.status_code == 503
    body = resp.json()
    assert body["detections"] == []
    assert "no monitor radio" in body["reason"] and "USB-UART Bridge" in body["reason"]


def test_the_panel_reads_stopped_when_marauder_never_answers(tmp_path, bridge):
    dec = MarauderFlockDetector(tty=bridge.path, answer_timeout=0.3)
    with _client(tmp_path, flock_factory=lambda: dec) as client:
        first = client.get("/api/flock").json()
        assert first["state"] == "starting" and first["command"] == "sniffprobe -serial"
        dec.drain()
        body = client.get("/api/flock").json()
    assert body["available"] is False
    assert body["state"] == "stopped"
    assert body["reason"].startswith(f"no answer from Marauder on {bridge.path}")


# C30: after a link died, a missing bridge port read STOPPED "· retrying now" for as long as it
# stayed missing, though nothing was retrying: the port check sits before the back-off, so no new
# link ever came to end the death's record. Only the death reads STOPPED; no port, or two, read as
# what they are, and the port coming back starts the link again.
def test_after_a_marauder_death_a_missing_bridge_reads_no_radio_not_stopped(tmp_path, bridge):
    ports = [bridge.path]
    dec = MarauderFlockDetector(glob=lambda pattern: list(ports), answer_timeout=0.05)
    now = [5000.0]
    dec.watch.clock = lambda: now[0]
    with _client(tmp_path, flock_factory=lambda: dec) as client:
        assert client.get("/api/flock").json()["state"] == "starting"
        dec.drain()                         # Marauder never answers on the pty: the link dies
        died = client.get("/api/flock").json()
        ports.clear()                       # the Flipper is unplugged
        gone = [client.get("/api/flock").json()]
        now[0] += 10 * childproc.MAX_RETRY_S
        gone += [client.get("/api/flock").json() for _ in range(3)]
        ports[:] = TWO_BRIDGES["by-id3"]
        two = client.get("/api/flock").json()
        ports[:] = [bridge.path]            # the bridge is back
        back = client.get("/api/flock").json()
        dec.stop()
    assert died["state"] == "stopped" and died["retry_in"] > 0, died
    assert died["reason"].startswith(f"no answer from Marauder on {bridge.path}")
    for body in gone:
        assert body["reason"].startswith("no monitor radio: "), body
        assert "state" not in body and "retry_in" not in body, body
    assert two["reason"].startswith("more than one Flipper USB-UART bridge"), two
    assert "state" not in two and "retry_in" not in two, two
    assert back["available"] is True and back["state"] == "starting", back
    assert dec.watch.stopped is False

    shown = _panel_shows("flock", [died, *gone, two])
    assert shown[0]["badge"] == "STOPPED"
    assert shown[0]["reason"] == died["reason"] + " · retrying in 2 s"
    for down, body in zip(shown[1:-1], gone):
        assert down["badge"] == "NO RADIO", down
        assert down["reason"] == body["reason"], down
    assert shown[-1]["badge"] == "UNAVAILABLE", shown[-1]
    assert shown[-1]["reason"] == two["reason"], shown[-1]


# Lesson 18's panel is 124 px tall: under its head it holds two 18 px lines of message, and the
# no-bridge refusal's third line was cut off (20.67 px of it, measured at 800x480). A line holds 66
# characters: 738 px of text (the 764 px panel less its borders and padding) at 11.16 px a
# character (PT Mono's 0.6 em advance plus the body's 0.02 em letter-spacing, at 18 px).
PANEL_MESSAGE_CHARS = 66
PANEL_MESSAGE_LINES = 2


def test_the_no_bridge_refusal_fits_the_lesson_panels_two_message_lines(tmp_path):
    dec = MarauderFlockDetector(glob=lambda pattern: [])
    with _client(tmp_path, flock_factory=lambda: dec) as client:
        body = client.get("/api/flock").json()
    assert body["available"] is False and "state" not in body   # refused, not a stopped link
    lines = textwrap.wrap(body["reason"], PANEL_MESSAGE_CHARS)
    assert len(lines) <= PANEL_MESSAGE_LINES, lines
    assert "GPIO → USB-UART Bridge, USB Channel 1" in body["reason"]   # still says how


# A site may pin the bridge's port in SIGINT101_MARAUDER_TTY, and a by-id name alone runs to some
# 50 to 60 characters: spelled out in the refusal, a pinned path ran it to a third line, which
# lesson 18's panel cut off. Whatever the site pins, the refusal keeps to the panel's two lines.
# Each path here sits under tmp_path, so no real port is ever touched.
@pytest.mark.parametrize("pinned", [
    "serial/by-id/usb-Flipper_Devices_Inc._Flipper_Ex4mple_flip_Ex4mple-if02",
    "ttyACM1",
], ids=["by-id", "acm"])
def test_a_pinned_port_keeps_the_refusal_in_the_lesson_panels_two_message_lines(
        tmp_path, monkeypatch, pinned):
    monkeypatch.setenv("SIGINT101_FLOCK_SOURCE", "marauder")
    monkeypatch.setenv("SIGINT101_MARAUDER_TTY", str(tmp_path / pinned))
    with _client(tmp_path) as client:
        body = client.get("/api/flock").json()
    assert body["available"] is False and "state" not in body   # refused, not a stopped link
    lines = textwrap.wrap(body["reason"], PANEL_MESSAGE_CHARS)
    assert len(lines) <= PANEL_MESSAGE_LINES, lines
    assert "GPIO → USB-UART Bridge, USB Channel 1" in body["reason"]   # still says how


# The refusal says only what the console looked for. The by-id name exists only while the bridge
# runs on USB channel 1, and the bridge starts on channel 0 whenever it is opened afresh
# (flipperzero-firmware 1.5.1 gpio_scene_usb_uart.c:31-38, usb_uart_bridge.c:107-115): "no
# bridge" would be false while it runs there. A pinned path may just be the wrong one, so the
# refusal names the variable that pinned it; spelled out, the path would not fit (the test above).
@pytest.mark.parametrize("pinned", [False, True], ids=["by-id", "pinned"])
def test_the_no_port_refusal_names_what_it_looked_for(tmp_path, pinned):
    path = str(tmp_path / "ttyACM1")
    dec = (MarauderFlockDetector(tty=path) if pinned
           else MarauderFlockDetector(glob=lambda pattern: []))
    with pytest.raises(FlockUnavailable) as refused:
        dec.start()
    looked_for = str(refused.value).split(". ", 1)[0]   # what it found missing, before the remedy
    assert looked_for.startswith("no monitor radio: ")
    if pinned:
        assert "SIGINT101_MARAUDER_TTY" in looked_for, looked_for
    else:
        assert re.search(r"\bUSB Channel 1\b", looked_for), looked_for


# Every refusal the Marauder path can show must be whole in lesson 18's panel: as many lines as the
# panel shows for a refusal (refusal_lines_shown, read from app.css), at the panel's 66 characters.
# A refusal is served before any link died and after one did. After one did it is still a refusal,
# not STOPPED with a countdown: only the death itself reads STOPPED, so the panel shows it as it
# shows a refusal before any death (C30, and the route test of it in test_app_radio_routes). Port
# names reach the detector through its glob, never as a real pin, and every open is doubled or
# lands on the test's pty, so no test opens a /dev/serial name. A by-id name is 44 + 2n characters
# for an n-character Flipper name, and the firmware caps n at 8.
BY_ID = "/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_{0}_flip_{0}-if02"
REFUSAL_PORTS = {
    "by-id3": BY_ID.format("Aaa"),
    "by-id8": BY_ID.format("Abcdefgh"),
    "by-path": "/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.2",
    "acm": "/dev/ttyACM1",
}
TWO_BRIDGES = {
    "by-id3": (BY_ID.format("Aaa"), BY_ID.format("Bbb")),
    "by-id8": (BY_ID.format("Abcdefgh"), BY_ID.format("Hgfedcba")),
}
REMEDY = "GPIO → USB-UART Bridge, USB Channel 1"
OPEN_CAUSES = ("eacces", "ebusy", "enoent", "eio", "termios-eio")
WRITE_CAUSES = ("eio", "eagain")
ERRNOS = {"eacces": errno.EACCES, "ebusy": errno.EBUSY, "enoent": errno.ENOENT,
          "eio": errno.EIO, "eagain": errno.EAGAIN}


def _cause(kind, path=None):
    """The error a port gives: an OSError as os.open raises it (with the path), or termios's."""
    if kind == "termios-eio":
        return termios.error(errno.EIO, os.strerror(errno.EIO))
    code = ERRNOS[kind]
    return OSError(code, os.strerror(code), path) if path else OSError(code, os.strerror(code))


def _system_words(exc):
    """What the system said, taken from the exception itself."""
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    if isinstance(exc, termios.error) and len(exc.args) == 2:
        return exc.args[1]
    return str(exc)


def _panel_text(body):
    """The refusal as lesson 18's panel shows it; a stopped one with the longest countdown."""
    if body.get("state") == "stopped":
        return f"{body['reason']} · retrying in {math.ceil(childproc.MAX_RETRY_S)} s"
    return body["reason"]


REFUSAL_CASES = []
for _form in ("never-died", "after-death"):
    REFUSAL_CASES += [("no-port-search", None, None, _form), ("no-port-pinned", None, None, _form)]
    REFUSAL_CASES += [("two-bridges", port, None, _form) for port in TWO_BRIDGES]
    REFUSAL_CASES += [("cannot-open", port, cause, _form)
                      for port in REFUSAL_PORTS for cause in OPEN_CAUSES]
    REFUSAL_CASES += [("cannot-write", port, cause, _form)
                      for port in REFUSAL_PORTS for cause in WRITE_CAUSES]
REFUSAL_CASES += [(row, port, None, "the-death")
                  for row in ("no-answer", "link-closed") for port in REFUSAL_PORTS]
REFUSAL_CASES += [(row, "by-id8", None, "the-death") for row in ("restarted", "linktype")]


def _served_refusal(tmp_path, monkeypatch, bridge, row, port, cause, form):
    """Drive the console to one refusal; return its /api/flock body and the words it must keep."""
    name = REFUSAL_PORTS.get(port) or REFUSAL_PORTS["by-id8"]
    names = [] if row == "no-port-search" and form == "never-died" else [name]
    real_open = _open_marauder_port
    monkeypatch.setattr(flock_module, "_open_marauder_port", lambda path: real_open(bridge.path))
    quick = row == "no-answer" or form == "after-death"
    if row == "no-port-pinned":
        pin = tmp_path / "ttyACM1"
        if form == "after-death":
            pin.symlink_to(bridge.path)
        dec = MarauderFlockDetector(tty=str(pin), answer_timeout=0.05)
    else:
        dec = MarauderFlockDetector(glob=lambda pattern: list(names),
                                    answer_timeout=0.05 if quick else 5.0)
    with _client(tmp_path, flock_factory=lambda: dec) as client:
        if form != "never-died":
            first = client.get("/api/flock").json()
            assert first["available"] is True, first
        if form == "after-death":
            dec.drain()   # Marauder never answers on the pty: the link dies
            died = client.get("/api/flock").json()
            assert died.get("state") == "stopped" and died["reason"].startswith("no answer"), died
        words = [name]
        if row == "no-port-search":
            names.clear()
            words = [REMEDY]
        elif row == "no-port-pinned":
            if pin.is_symlink():
                pin.unlink()
            words = [REMEDY]
        elif row == "two-bridges":
            names[:] = TWO_BRIDGES[port]
            words = [os.path.basename(p) for p in TWO_BRIDGES[port]]   # as the refusal names them
        elif row == "cannot-open":
            exc = _cause(cause, name)

            def refuse_open(path):
                raise exc
            monkeypatch.setattr(flock_module, "_open_marauder_port", refuse_open)
            words = [name, _system_words(exc)]
        elif row == "cannot-write":
            exc = _cause(cause)

            def refuse_write(self, line):
                raise exc
            monkeypatch.setattr(MarauderLink, "send", refuse_write)
            words = [name, _system_words(exc)]
        elif row == "no-answer":
            dec.drain()
        elif row == "link-closed":
            bridge.hang_up()
            dec.drain()
        elif row == "restarted":
            bridge.say(marauder_stream([]))
            wait_for(lambda: dec._proc.answered)
            bridge.say(b"ESP-IDF version is: v4.4.4\r\n")
            dec.drain()
            words = ["restarted"]
        elif row == "linktype":
            bridge.say(MARAUDER_STARTUP + marauder_chunk(marauder_header(linktype=1)))
            dec.drain()
            words = ["linktype"]
        if form == "after-death" and row in ("cannot-open", "cannot-write"):
            time.sleep(0.15)   # reached only once the back-off has passed
        body = client.get("/api/flock").json()
    return body, words


@pytest.mark.parametrize("row,port,cause,form", REFUSAL_CASES,
                         ids=["-".join(p for p in case if p) for case in REFUSAL_CASES])
def test_every_marauder_refusal_shows_whole_in_the_lesson_panel(
        tmp_path, monkeypatch, bridge, row, port, cause, form):
    monkeypatch.setattr(flock_module, "LINK_POLL_S", 0.01)
    monkeypatch.setattr(childproc, "FIRST_RETRY_S", 0.05)
    body, words = _served_refusal(tmp_path, monkeypatch, bridge, row, port, cause, form)
    assert body["available"] is False, body
    if form == "the-death":
        assert body.get("state") == "stopped", body
    else:
        assert "state" not in body, body       # refused, never stopped, before a death or after
    text = _panel_text(body)
    lines = textwrap.wrap(text, PANEL_MESSAGE_CHARS)
    assert len(lines) <= refusal_lines_shown(), lines
    for word in words:
        assert word in text, (word, text)


# "cannot open" names the port once, whatever the error that stopped the open carries: os.open's
# OSError repeats the path it was given, and named twice a by-id name ran the refusal past the lines
# the panel shows. The port comes from the detector, so no form may lose it either.
CANNOT_OPEN_FORMS = ("eacces", "ebusy", "enoent", "eio", "no-filename", "message-only",
                     "termios-eio", "real-missing-path", "real-regular-file")


@pytest.mark.parametrize("form", CANNOT_OPEN_FORMS)
def test_every_cannot_open_names_the_port_once(tmp_path, monkeypatch, form):
    if form == "real-missing-path":
        port = str(tmp_path / "ttyACM-missing")        # os.open raises FileNotFoundError
    elif form == "real-regular-file":
        port = str(tmp_path / "ttyACM-file")           # tty.setraw raises termios.error 25
        Path(port).write_bytes(b"")
    else:
        port = REFUSAL_PORTS["by-id8"]
        exc = {"no-filename": OSError(errno.EIO, os.strerror(errno.EIO)),
               "message-only": OSError("device went away")}.get(form) or _cause(form, port)

        def refuse_open(path):
            raise exc
        monkeypatch.setattr(flock_module, "_open_marauder_port", refuse_open)
    dec = MarauderFlockDetector(glob=lambda pattern: [port])
    with pytest.raises(FlockUnavailable) as refused:
        dec.start()
    text = str(refused.value)
    cause = refused.value.__cause__
    assert text.startswith("cannot open "), text
    assert text.count(port) == 1, text
    assert _system_words(cause) in text, text
    # A cause with an error number reads the same whichever kind it is: termios.error is no OSError,
    # and as itself it showed a Python tuple, "(25, 'Inappropriate ioctl for device')".
    code = cause.errno if isinstance(cause, OSError) else (
        cause.args[0] if isinstance(cause, termios.error) and len(cause.args) == 2 else None)
    if code is not None:
        assert f"[Errno {code}] {_system_words(cause)}" in text, text


# --------------------------------------------------------------------------- the logged reception

# A Flock reception's record names the tool that heard it beside the command that tool ran. Under
# the Marauder source it said "tcpdump" beside "sniffprobe -serial": the rack instrument's default
# tool, not the configured detector's.
CAMERA_PROBE = mgmt_frame(4, BROADCAST, FLOCK_OUI + b"\x00\x00\x13", BROADCAST, FLOCK_IES)


def _flock_receptions(client):
    """The Flock receptions the console logged, newest first, as the rack's LOG line reads them."""
    body = client.get("/api/receptions?instrument=flock").json()
    return [event["payload"] for event in body["receptions"]]


def test_a_reception_off_wlan1_is_logged_with_the_tool_its_command_runs(tmp_path, monkeypatch):
    # tcpdump's own argv, launched through the product's spawn; only the process is a stand-in.
    def spawn(argv, **kw):
        child = Child(argv)
        child.stdout = io.BytesIO(pcap_stream([CAMERA_PROBE]))
        return child

    monkeypatch.setattr("sigint101.flock.spawn", spawn)
    dec = FlockDetector(radio_present=lambda: True, which=lambda name: "/usr/bin/" + name)
    with _client(tmp_path, flock_factory=lambda: dec) as client:
        assert client.get("/api/flock").status_code == 200
        dec.drain()
        [record] = _flock_receptions(client)
    assert record["command"].startswith("tcpdump -i wlan1 ")
    assert record["tool"] == shlex.split(record["command"])[0] == "tcpdump"


def test_a_reception_off_marauder_is_logged_with_the_tool_its_command_runs(tmp_path, bridge):
    dec = MarauderFlockDetector(tty=bridge.path)
    with _client(tmp_path, flock_factory=lambda: dec) as client:
        assert client.get("/api/flock").status_code == 200
        bridge.heard()
        bridge.say(marauder_stream([CAMERA_PROBE]))
        wait_for(lambda: _flock_receptions(client))
        [record] = _flock_receptions(client)
    # sniffprobe is a command typed into Marauder's console, not a program: Marauder runs it.
    assert record["command"] == "sniffprobe -serial"
    assert record["tool"] == "Marauder"
