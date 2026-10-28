# ABOUTME: Tests the ADS-B backend — parsing SBS lines, tracking aircraft, range/bearing geometry.
# ABOUTME: The dump1090 feed is a fake at the boundary; no radio / no dump1090 is honest, not faked.

import pytest

from sigint101.adsb import (
    DEFAULT_ADSB_HZ,
    AdsbDecoder,
    AdsbUnavailable,
    AircraftTracker,
    great_circle,
    parse_sbs_message,
)

# Real dump1090-fa BaseStation (SBS-1) lines. The field layout, the trailing 'H' on a geometric
# altitude, the 8-char space-padded callsign and the -1/0 flags are exactly what net_io.c emits;
# one aircraft (hex A1B2C3) sends its identity (MSG,1), position (MSG,3) and velocity (MSG,4).
IDENT = ("MSG,1,1,1,A1B2C3,1,2026/10/28,10:15:00.000,2026/10/28,10:15:00.100,"
         "UAL123  ,,,,,,,,0,0,0,0")
POSITION = ("MSG,3,1,1,A1B2C3,1,2026/10/28,10:15:01.000,2026/10/28,10:15:01.100,"
            ",37000,,,37.61950,-122.38680,,,0,0,0,0")
VELOCITY = ("MSG,4,1,1,A1B2C3,1,2026/10/28,10:15:02.000,2026/10/28,10:15:02.100,"
            ",,450,118,,,-832,,0,0,0,0")
# A geometric altitude carries a trailing 'H'; a surveillance-ID message carries the squawk.
POS_GEOM = ("MSG,3,1,1,DDEEFF,1,2026/10/28,10:15:03.000,2026/10/28,10:15:03.100,"
            ",39000H,,,37.80000,-122.40000,,,0,0,0,0")
SQUAWK = ("MSG,6,1,1,A1B2C3,1,2026/10/28,10:15:04.000,2026/10/28,10:15:04.100,"
          ",12000,,,,,,2200,0,0,0,0")


def test_parse_identity_reads_the_callsign():
    m = parse_sbs_message(IDENT)
    assert m is not None
    assert m.msg_type == 1
    assert m.hex_ident == "A1B2C3"
    assert m.callsign == "UAL123"          # trailing pad stripped
    assert m.altitude_ft is None


def test_parse_position_reads_altitude_and_latlon():
    m = parse_sbs_message(POSITION)
    assert m.msg_type == 3
    assert m.altitude_ft == 37000
    assert m.lat == pytest.approx(37.61950)
    assert m.lon == pytest.approx(-122.38680)
    assert m.callsign is None


def test_parse_strips_the_geometric_altitude_suffix():
    m = parse_sbs_message(POS_GEOM)
    assert m.altitude_ft == 39000          # the trailing 'H' is not part of the number
    assert m.hex_ident == "DDEEFF"


def test_parse_velocity_reads_speed_and_track():
    m = parse_sbs_message(VELOCITY)
    assert m.msg_type == 4
    assert m.ground_speed_kt == pytest.approx(450)
    assert m.track_deg == pytest.approx(118)


def test_parse_reads_the_squawk():
    m = parse_sbs_message(SQUAWK)
    assert m.squawk == "2200"


def test_parse_skips_non_message_lines():
    assert parse_sbs_message("") is None
    assert parse_sbs_message("not,a,sbs,line") is None
    # dump1090 also emits STA/ID/CLK records; they are not aircraft messages.
    assert parse_sbs_message("STA,,1,1,A1B2C3,1,,,,,,,,,,,,,,,,") is None
    assert parse_sbs_message("ID,,1,1,A1B2C3,1,,,,,UAL123,,,,,,,,,,,") is None


def test_parse_rejects_a_message_with_no_hex_ident():
    broken = IDENT.replace("A1B2C3", "")
    assert parse_sbs_message(broken) is None


def test_tracker_merges_identity_position_and_velocity():
    t = AircraftTracker()
    for line in (IDENT, POSITION, VELOCITY):
        t.absorb(parse_sbs_message(line), now=100.0)
    planes = t.aircraft(now=100.0)
    assert len(planes) == 1
    a = planes[0]
    assert a.hex_ident == "A1B2C3"
    assert a.callsign == "UAL123"
    assert a.altitude_ft == 37000
    assert a.ground_speed_kt == pytest.approx(450)
    assert a.lat == pytest.approx(37.61950)
    assert a.message_count == 3


def test_tracker_separates_two_aircraft():
    t = AircraftTracker()
    t.absorb(parse_sbs_message(POSITION), now=1.0)
    t.absorb(parse_sbs_message(POS_GEOM), now=1.0)
    assert {a.hex_ident for a in t.aircraft(now=1.0)} == {"A1B2C3", "DDEEFF"}


def test_tracker_prunes_aircraft_not_heard_recently():
    t = AircraftTracker()
    t.absorb(parse_sbs_message(POSITION), now=0.0)
    # Heard at t=0; by t=120s with a 60s window it has aged out of the live picture.
    assert t.aircraft(now=120.0, max_age_s=60.0) == []


def test_tracker_keeps_a_field_when_a_later_message_omits_it():
    t = AircraftTracker()
    t.absorb(parse_sbs_message(POSITION), now=1.0)   # sets altitude + position
    t.absorb(parse_sbs_message(VELOCITY), now=2.0)   # carries no altitude
    a = t.aircraft(now=2.0)[0]
    assert a.altitude_ft == 37000                    # not clobbered by the velocity message


def test_tracker_ignores_a_none_message():
    t = AircraftTracker()
    t.absorb(None, now=1.0)
    assert t.aircraft(now=1.0) == []


def test_great_circle_one_degree_of_latitude_is_sixty_nautical_miles():
    dist, bearing = great_circle(0.0, 0.0, 1.0, 0.0)
    assert dist == pytest.approx(60.0, abs=0.1)
    assert bearing == pytest.approx(0.0, abs=0.5)    # due north


def test_great_circle_one_degree_of_longitude_at_equator_bears_east():
    dist, bearing = great_circle(0.0, 0.0, 0.0, 1.0)
    assert dist == pytest.approx(60.0, abs=0.1)
    assert bearing == pytest.approx(90.0, abs=0.5)   # due east


class FakeFeed:
    """Stands in for the dump1090 process + SBS socket at the boundary."""

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
    return AdsbDecoder(spawn=lambda device: FakeFeed(lines), **kw)


def test_decoder_builds_the_picture_from_the_feed():
    dec = _decoder([IDENT, "garbage line", POSITION, VELOCITY])
    dec.start()
    dec.drain()
    planes = dec.aircraft()
    assert len(planes) == 1
    assert planes[0]["callsign"] == "UAL123"
    assert planes[0]["altitude_ft"] == 37000


def test_decoder_computes_range_and_bearing_from_the_receiver():
    # Receiver one degree of latitude south of the aircraft -> ~60 nm due north.
    dec = _decoder([POSITION], receiver_lat=36.61950, receiver_lon=-122.38680)
    dec.start()
    dec.drain()
    a = dec.aircraft()[0]
    assert a["range_nm"] == pytest.approx(60.0, abs=0.5)
    assert a["bearing_deg"] == pytest.approx(0.0, abs=1.0)


def test_decoder_leaves_range_none_without_a_receiver_location():
    dec = _decoder([POSITION])
    dec.start()
    dec.drain()
    a = dec.aircraft()[0]
    assert a["range_nm"] is None and a["bearing_deg"] is None


def test_decoder_refuses_without_a_radio():
    dec = _decoder([], radio_present=lambda: False)
    with pytest.raises(AdsbUnavailable):
        dec.start()


def test_decoder_refuses_when_dump1090_is_missing():
    dec = _decoder([], which=lambda name: None)
    with pytest.raises(AdsbUnavailable):
        dec.start()


def test_decoder_refuses_once_released():
    dec = _decoder([POSITION])
    dec.release()
    with pytest.raises(AdsbUnavailable):
        dec.start()
    dec.resume()
    dec.start()
    dec.drain()
    assert dec.aircraft()[0]["hex_ident"] == "A1B2C3"


def test_release_clears_the_picture():
    dec = _decoder([POSITION])
    dec.start()
    dec.drain()
    assert dec.aircraft()
    dec.release()
    assert dec.aircraft() == []


def test_stop_reaps_the_feed_and_releases_the_dongle():
    box = {}

    def spawn(device):
        box["f"] = FakeFeed([POSITION])
        return box["f"]

    dec = AdsbDecoder(spawn=spawn, radio_present=lambda: True,
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

    dec = AdsbDecoder(spawn=lambda d: RaisingFeed(), radio_present=lambda: True,
                      which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "socket died"


def test_default_adsb_frequency_is_1090_mhz():
    assert DEFAULT_ADSB_HZ == 1_090_000_000
