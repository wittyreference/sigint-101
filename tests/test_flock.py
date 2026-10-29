# ABOUTME: Tests the Flock ALPR detector — parsing real 802.11 mgmt frames and matching signatures.
# ABOUTME: Fixtures are genuine off-air captures from nomad-b's wlan1; the tcpdump process is faked.

import io
import re
import struct
from pathlib import Path

import pytest

from sigint101.flock import (
    FLOCK_IE_SIGNATURE,
    FLOCK_OUIS,
    FlockDetector,
    FlockRoster,
    FlockUnavailable,
    iter_pcap_frames,
    match_signatures,
    parse_management_frame,
)

# A real off-air capture: tcpdump -i wlan1 -s 512 -w off wlan1 in monitor mode, 2026-10-29.
# 207 management frames, 63 of them probe requests (random_eero, Fellow_OTA, DIRECT-MBUX, ...).
# No Flock ALPR camera was in range when it was taken, so every frame here is a true negative —
# the honest baseline a detector must not turn into a false alarm.
FIXTURE = Path(__file__).parent / "fixtures" / "flock_ch1.pcap"

# 34 Flock OUIs from colonelpanichacks/flock-you @6704f01 main.cpp (32 community + 2 firmware).
FLOCK_OUI = bytes.fromhex("b41e52")          # Flock's own IEEE block
FLOCK_OUI_LOCALLY_ADMIN = bytes.fromhex("826bf2")   # has the LA bit set — must still match
BROADCAST = b"\xff\xff\xff\xff\xff\xff"
NON_FLOCK = bytes.fromhex("ac:f1:08".replace(":", ""))   # a real eero OUI from the fixture

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


def test_iter_pcap_frames_reads_every_record_from_the_real_capture():
    with open(FIXTURE, "rb") as fh:
        frames = list(iter_pcap_frames(fh))
    assert len(frames) == 207


def test_parses_real_probe_requests_with_sane_macs_and_signatures():
    with open(FIXTURE, "rb") as fh:
        infos = [parse_management_frame(f) for f in iter_pcap_frames(fh)]
    probes = [i for i in infos if i is not None and i.is_probe_request]
    assert len(probes) == 63

    srcs = {p.src for p in probes}
    assert "ac:f1:08:70:a3:ea" in srcs          # a real randomised eero MAC
    assert "60:ab:14:30:8a:47" in srcs
    assert all(MAC_RE.match(s) for s in srcs)

    # The randomised eero probes all carry supported-rates (1) + extended rates (50); their third
    # element is a truncated tag whose id the fingerprint emits verbatim, so they share the 1,50
    # prefix. The documented example 1,50,71 is one of them.
    eero = [p for p in probes if p.ssid == "random_eero"]
    assert len(eero) > 40
    assert all(p.ie_signature.startswith("1,50") for p in eero)
    assert any(p.ie_signature == "1,50,71" for p in eero)


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


def test_real_eero_probe_is_not_a_flock_hit():
    with open(FIXTURE, "rb") as fh:
        infos = [parse_management_frame(f) for f in iter_pcap_frames(fh)]
    probes = [i for i in infos if i is not None and i.is_probe_request]
    assert probes
    assert all(match_signatures(p, ssid_match_enabled=True) is None for p in probes)


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
