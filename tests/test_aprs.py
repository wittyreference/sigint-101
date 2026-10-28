# ABOUTME: Tests the APRS backend — parsing multimon-ng TNC2 lines into stations with position/text.
# ABOUTME: The rtl_fm|multimon-ng pipeline is a fake at the boundary; no radio / no tool is honest.

import pytest

from sigint101.aprs import (
    DEFAULT_APRS_HZ,
    AprsDecoder,
    AprsUnavailable,
    StationRoster,
    parse_aprs_frame,
    parse_aprs_line,
)

# Real multimon-ng AFSK1200 output lines (the TNC2 monitor format it prints after "APRS: " —
# SRC>DST,DIGI,...:info, SSID shown only when nonzero, a repeated digi carrying a trailing '*').
POS = "APRS: KD2ABC-9>APU25N,WIDE1-1,WIDE2-1:!4042.00N/07400.00W>Test de KD2ABC"
MSG = "APRS: K9XYZ>APRS::KD2ABC   :Hello there{001"
STATUS = "APRS: N0CALL>APRS,WIDE1-1*:>Monitoring 146.52"
# A position with a timestamp (@) — the 7-char time group sits before the latitude.
POS_TS = "APRS: W1AW-1>APU25N,WIDE2-1:@092345z4225.50N/07115.20W-Hartford"


def test_parse_line_strips_the_prefix():
    assert parse_aprs_line(POS) == POS[len("APRS: "):]      # the TNC2 frame, prefix removed


def test_parse_line_skips_non_aprs_output():
    # multimon-ng also prints other demods and status noise; only APRS: lines are frames.
    assert parse_aprs_line("") is None
    assert parse_aprs_line("Enabled demodulators: AFSK1200") is None
    assert parse_aprs_line("POCSAG1200: Address: 123") is None


def test_parse_frame_reads_the_ax25_addressing():
    p = parse_aprs_frame(parse_aprs_line(POS))
    assert p is not None
    assert p.source == "KD2ABC-9"
    assert p.dest == "APU25N"
    assert p.path == ["WIDE1-1", "WIDE2-1"]


def test_parse_frame_reads_an_uncompressed_position():
    p = parse_aprs_frame(parse_aprs_line(POS))
    assert p.kind == "position"
    assert p.lat == pytest.approx(40.70, abs=0.01)     # 40 deg 42.00 min N
    assert p.lon == pytest.approx(-74.00, abs=0.01)    # 074 deg 00.00 min W
    assert p.text == "Test de KD2ABC"                  # the comment after the symbol code


def test_parse_frame_skips_the_timestamp_on_a_timestamped_position():
    p = parse_aprs_frame(parse_aprs_line(POS_TS))
    assert p.kind == "position"
    assert p.lat == pytest.approx(42.425, abs=0.01)    # 42 deg 25.50 min N
    assert p.lon == pytest.approx(-71.253, abs=0.01)   # 071 deg 15.20 min W
    assert p.text == "Hartford"


def test_parse_frame_reads_a_message():
    p = parse_aprs_frame(parse_aprs_line(MSG))
    assert p.kind == "message"
    assert p.addressee == "KD2ABC"                     # 9-char field, trimmed
    assert p.text == "Hello there"                     # the {001 line number is stripped


def test_parse_frame_reads_a_status_and_a_repeated_digi():
    p = parse_aprs_frame(parse_aprs_line(STATUS))
    assert p.kind == "status"
    assert p.text == "Monitoring 146.52"
    assert p.path == ["WIDE1-1*"]                      # the '*' marks the digi that repeated it


def test_parse_frame_rejects_a_line_with_no_info():
    assert parse_aprs_frame("KD2ABC>APRS") is None     # no ':' — not a full frame


def test_roster_keeps_the_latest_packet_per_station():
    r = StationRoster()
    r.absorb(parse_aprs_frame(parse_aprs_line(POS)), now=1.0)
    r.absorb(parse_aprs_frame(parse_aprs_line(STATUS)), now=2.0)
    r.absorb(parse_aprs_frame(
        "KD2ABC-9>APU25N:!4042.50N/07400.50W>moved"), now=3.0)   # same station, newer
    stations = r.stations(now=3.0)
    assert len(stations) == 2                          # KD2ABC-9 and N0CALL, not three rows
    kd = next(s for s in stations if s.source == "KD2ABC-9")
    assert kd.text == "moved"
    assert kd.packet_count == 2


def test_roster_orders_freshest_first():
    r = StationRoster()
    r.absorb(parse_aprs_frame(parse_aprs_line(POS)), now=1.0)
    r.absorb(parse_aprs_frame(parse_aprs_line(STATUS)), now=5.0)
    assert r.stations(now=5.0)[0].source == "N0CALL"


def test_roster_ignores_a_none_packet():
    r = StationRoster()
    r.absorb(None, now=1.0)
    assert r.stations(now=1.0) == []


class FakePipeline:
    """Stands in for the rtl_fm|multimon-ng pipeline at the process boundary."""

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
    return AprsDecoder(spawn=lambda freq, device: FakePipeline(lines), **kw)


def test_decoder_builds_the_roster_from_the_pipeline():
    dec = _decoder([POS, "noise line", STATUS, MSG])
    dec.start()
    dec.drain()
    stations = dec.stations()
    assert {s["source"] for s in stations} == {"KD2ABC-9", "N0CALL", "K9XYZ"}


def test_decoder_refuses_without_a_radio():
    dec = _decoder([], radio_present=lambda: False)
    with pytest.raises(AprsUnavailable):
        dec.start()


def test_decoder_refuses_when_multimon_is_missing():
    dec = _decoder([], which=lambda name: None if name == "multimon-ng" else "/usr/bin/" + name)
    with pytest.raises(AprsUnavailable):
        dec.start()


def test_decoder_refuses_once_released():
    dec = _decoder([POS])
    dec.release()
    with pytest.raises(AprsUnavailable):
        dec.start()
    dec.resume()
    dec.start()
    dec.drain()
    assert dec.stations()[0]["source"] == "KD2ABC-9"


def test_release_clears_the_roster():
    dec = _decoder([POS])
    dec.start()
    dec.drain()
    assert dec.stations()
    dec.release()
    assert dec.stations() == []


def test_a_reader_error_is_recorded_not_swallowed():
    class RaisingPipeline(FakePipeline):
        def __init__(self):
            def boom():
                raise OSError("pipe died")
                yield  # pragma: no cover
            self.stdout = boom()
            self.terminated = False

    dec = AprsDecoder(spawn=lambda f, d: RaisingPipeline(), radio_present=lambda: True,
                      which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "pipe died"


def test_default_aprs_frequency_is_the_2m_calling_channel():
    assert DEFAULT_APRS_HZ == 144_390_000
