# ABOUTME: Tests the AIS backend — AIVDM 6-bit decode, multi-part reassembly, the vessel roster.
# ABOUTME: The rtl_ais feed is a fake at the boundary; no radio / no rtl_ais is honest, not faked.

import pytest

from sigint101.ais import (
    AIS_CHANNEL_A_HZ,
    AIS_CHANNEL_B_HZ,
    DEFAULT_AIS_PORT,
    AisAssembler,
    AisDecoder,
    AisReport,
    AisUnavailable,
    VesselRoster,
    decode_payload,
    parse_ais_sentence,
)

# Published AIVDM test vectors with known decodes.
#
# TYPE1 is the single-sentence position report used as the worked example in the gpsd
# AIVDM/AIVDO spec (https://gpsd.gitlab.io/gpsd/AIVDM.html). TYPE5_FRAG1/FRAG2 are the
# two-fragment static-and-voyage message from the gpsd regression corpus (test/sample.aivdm,
# contributed by Kurt Schwehr, checked with the noaadata tools and the Maritec decoder); its
# decode is recorded in test/sample.aivdm.chk as
#   5|0|351759000|9134270|0|3FOF8|EVER DIADEM|70|...|NEW YORK|0
TYPE1 = "!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C"
TYPE5_FRAG1 = "!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0*1C"
TYPE5_FRAG2 = "!AIVDM,2,2,1,A,88888888880,2*25"


def test_parse_sentence_reads_the_fragment_fields():
    s = parse_ais_sentence(TYPE1)
    assert s is not None
    assert s.frag_count == 1
    assert s.frag_num == 1
    assert s.channel == "B"
    assert s.seq_id is None              # a single-sentence message has no sequence id
    assert s.payload.startswith("177KQJ")
    assert s.fill == 0


def test_parse_sentence_reads_a_multipart_header():
    s = parse_ais_sentence(TYPE5_FRAG2)
    assert s.frag_count == 2
    assert s.frag_num == 2
    assert s.seq_id == "1"
    assert s.fill == 2                   # the final fragment carries the pad count


def test_parse_sentence_rejects_a_non_aivdm_line():
    assert parse_ais_sentence("") is None
    assert parse_ais_sentence("not a sentence") is None
    assert parse_ais_sentence("$GPGGA,123519,4807.038,N*47") is None


def test_parse_sentence_rejects_a_short_sentence():
    assert parse_ais_sentence("!AIVDM,1,1") is None          # no checksum, too few fields


def test_parse_sentence_rejects_a_bad_checksum():
    corrupt = TYPE1[:-2] + "00"          # keep the '*', wreck the two hex digits
    assert parse_ais_sentence(corrupt) is None


def test_decode_type1_position_report():
    s = parse_ais_sentence(TYPE1)
    r = decode_payload(s.payload, s.fill)
    assert r is not None
    assert r.msg_type == 1
    assert r.mmsi == 477553000
    assert r.nav_status == "Moored"
    assert r.sog_kt == pytest.approx(0.0)
    assert r.lat == pytest.approx(47.58283, abs=1e-4)
    assert r.lon == pytest.approx(-122.34583, abs=1e-4)
    assert r.cog_deg == pytest.approx(51.0)
    assert r.heading_deg == 181
    assert r.name is None                # a position report carries no static name


def test_decode_rejects_empty_and_truncated_payloads():
    assert decode_payload("") is None
    assert decode_payload("1") is None   # the 6 type bits only, no room for an MMSI


def test_decode_ignores_untracked_message_types():
    # A type-4 base-station report decodes structurally, but it is not a ship to roster.
    assert decode_payload("4000000") is None


def test_assembler_decodes_a_single_part_sentence():
    r = AisAssembler().push(TYPE1)
    assert r is not None
    assert r.msg_type == 1
    assert r.mmsi == 477553000


def test_assembler_reassembles_a_two_part_type5():
    asm = AisAssembler()
    assert asm.push(TYPE5_FRAG1) is None     # first fragment alone decodes to nothing
    r = asm.push(TYPE5_FRAG2)                 # the second completes the message
    assert r is not None
    assert r.msg_type == 5
    assert r.mmsi == 351759000
    assert r.name == "EVER DIADEM"
    assert r.call_sign == "3FOF8"
    assert r.ship_type == 70
    assert r.lat is None                     # static data carries no position


def test_assembler_ignores_a_bad_sentence():
    assert AisAssembler().push("not a sentence") is None


def test_roster_keeps_vessels_newest_first():
    roster = VesselRoster()
    roster.absorb(AisReport(msg_type=1, mmsi=111, lat=1.0, lon=1.0), now=1.0)
    roster.absorb(AisReport(msg_type=1, mmsi=222, lat=2.0, lon=2.0), now=2.0)
    assert [v.mmsi for v in roster.vessels()] == [222, 111]


def test_roster_limit_bounds_the_list():
    roster = VesselRoster()
    for i, mmsi in enumerate((111, 222, 333)):
        roster.absorb(AisReport(msg_type=1, mmsi=mmsi), now=float(i))
    assert len(roster.vessels(limit=2)) == 2


def test_roster_ignores_a_none_report():
    roster = VesselRoster()
    roster.absorb(None, now=1.0)
    assert roster.vessels() == []


def test_roster_merges_static_into_a_vessels_position():
    asm = AisAssembler()
    asm.push(TYPE5_FRAG1)
    static = asm.push(TYPE5_FRAG2)           # EVER DIADEM, mmsi 351759000, ship type 70
    roster = VesselRoster()
    roster.absorb(static, now=1.0)
    # A position report for the SAME vessel arrives afterwards.
    position = AisReport(msg_type=1, mmsi=static.mmsi, lat=47.58, lon=-122.34,
                         sog_kt=0.0, cog_deg=51.0, heading_deg=181, nav_status="Moored")
    roster.absorb(position, now=2.0)
    vessels = roster.vessels()
    assert len(vessels) == 1                 # one vessel, not two
    v = vessels[0]
    assert v.name == "EVER DIADEM"           # static name kept
    assert v.ship_type == 70                 # static type kept
    assert v.lat == pytest.approx(47.58)     # position merged in
    assert v.nav_status == "Moored"
    assert v.message_count == 2


def test_vessel_as_dict_has_the_api_keys():
    roster = VesselRoster()
    roster.absorb(AisReport(msg_type=1, mmsi=123456789, lat=1.0, lon=2.0), now=5.0)
    d = roster.vessels()[0].as_dict()
    assert set(d) == {
        "mmsi", "name", "lat", "lon", "sog_kt", "cog_deg", "heading_deg",
        "nav_status", "ship_type", "message_count", "last_seen",
    }
    assert d["mmsi"] == 123456789
    assert d["last_seen"] == 5.0


class FakeFeed:
    """Stands in for the rtl_ais process + UDP socket at the boundary (stdout is a line stream)."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _decoder(lines, **kw):
    kw.setdefault("radio_present", lambda: True)
    kw.setdefault("which", lambda name: "/usr/bin/" + name)
    return AisDecoder(spawn=lambda device, host, port: FakeFeed(lines), **kw)


def test_decoder_builds_the_roster_from_the_feed():
    dec = _decoder([TYPE1, "garbage", TYPE5_FRAG1, TYPE5_FRAG2])
    dec.start()
    dec.drain()
    vessels = dec.vessels()
    assert len(vessels) == 2
    by_mmsi = {v["mmsi"]: v for v in vessels}
    assert by_mmsi[477553000]["lat"] == pytest.approx(47.58283, abs=1e-3)
    assert by_mmsi[351759000]["name"] == "EVER DIADEM"
    assert by_mmsi[351759000]["ship_type"] == 70


def test_decoder_refuses_without_a_radio():
    dec = _decoder([], radio_present=lambda: False)
    with pytest.raises(AisUnavailable):
        dec.start()


def test_decoder_refuses_when_rtl_ais_is_missing():
    dec = _decoder([], which=lambda name: None if name == "rtl_ais" else "/usr/bin/" + name)
    with pytest.raises(AisUnavailable):
        dec.start()


def test_decoder_refuses_once_released():
    dec = _decoder([TYPE1])
    dec.release()
    with pytest.raises(AisUnavailable):
        dec.start()
    dec.resume()
    dec.start()
    dec.drain()
    assert dec.vessels()[0]["mmsi"] == 477553000


def test_release_clears_the_roster():
    dec = _decoder([TYPE1])
    dec.start()
    dec.drain()
    assert dec.vessels()
    dec.release()
    assert dec.vessels() == []


def test_stop_reaps_the_feed_and_releases_the_dongle():
    box = {}

    def spawn(device, host, port):
        box["f"] = FakeFeed([TYPE1])
        return box["f"]

    dec = AisDecoder(spawn=spawn, radio_present=lambda: True,
                     which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    dec.stop()
    assert box["f"].terminated is True
    assert dec.running is False


def test_a_reader_error_is_recorded_not_swallowed():
    class RaisingFeed(FakeFeed):
        def __init__(self):
            def boom():
                raise OSError("socket died")
                yield  # pragma: no cover

            self.stdout = boom()
            self.terminated = False

    dec = AisDecoder(spawn=lambda d, h, p: RaisingFeed(), radio_present=lambda: True,
                     which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "socket died"


def test_default_ais_channels_and_port():
    assert AIS_CHANNEL_A_HZ == 161_975_000
    assert AIS_CHANNEL_B_HZ == 162_025_000
    assert DEFAULT_AIS_PORT == 10110
