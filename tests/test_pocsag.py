# ABOUTME: Tests the POCSAG backend — parsing multimon-ng pager lines into addressed messages.
# ABOUTME: The rtl_fm|multimon-ng pipeline is a fake at the boundary; no radio / no tool is honest.

import pytest

from sigint101.pocsag import (
    DEFAULT_POCSAG_HZ,
    PageLog,
    PocsagDecoder,
    PocsagUnavailable,
    parse_pocsag_line,
)

# Real multimon-ng POCSAG output lines. The prefix carries the baud rate, the address is right-
# aligned in 7 columns, the function is one digit, and the content is labelled Alpha/Numeric (or
# absent for a tone-only page). Taken from multimon-ng's pocsag.c format strings.
ALPHA = "POCSAG1200: Address: 1234567  Function: 3  Alpha:   CALL EXT 2210 URGENT"
NUMERIC = "POCSAG1200: Address:  123456  Function: 0  Numeric: 1234567890"
TONE = "POCSAG512: Address: 1000000  Function: 1 "
NO_ADDR = "POCSAG1200: Address:       -  Function: -  Alpha:   fragment"


def test_parse_an_alphanumeric_page():
    p = parse_pocsag_line(ALPHA)
    assert p is not None
    assert p.baud == 1200
    assert p.address == 1234567
    assert p.function == 3
    assert p.kind == "alpha"
    assert p.text == "CALL EXT 2210 URGENT"


def test_parse_a_numeric_page():
    p = parse_pocsag_line(NUMERIC)
    assert p.baud == 1200
    assert p.address == 123456
    assert p.function == 0
    assert p.kind == "numeric"
    assert p.text == "1234567890"


def test_parse_a_tone_only_page_has_no_text():
    p = parse_pocsag_line(TONE)
    assert p.baud == 512
    assert p.address == 1000000
    assert p.function == 1
    assert p.kind == "tone"
    assert p.text is None


def test_parse_a_page_with_no_decoded_address():
    p = parse_pocsag_line(NO_ADDR)
    assert p.address is None
    assert p.function is None
    assert p.kind == "alpha"
    assert p.text == "fragment"


def test_parse_skips_non_pocsag_lines():
    assert parse_pocsag_line("") is None
    assert parse_pocsag_line("APRS: KD2ABC>APRS:>hi") is None
    assert parse_pocsag_line("Enabled demodulators: POCSAG1200") is None


def test_pagelog_keeps_recent_pages_newest_first():
    log = PageLog()
    log.add(parse_pocsag_line(NUMERIC))
    log.add(parse_pocsag_line(ALPHA))
    pages = log.recent()
    assert len(pages) == 2
    assert pages[0].text == "CALL EXT 2210 URGENT"   # newest first


def test_pagelog_limit_bounds_the_history():
    log = PageLog(maxlen=2)
    for _ in range(5):
        log.add(parse_pocsag_line(ALPHA))
    assert len(log.recent()) == 2


def test_pagelog_recent_honours_a_limit_argument():
    log = PageLog()
    for _ in range(4):
        log.add(parse_pocsag_line(ALPHA))
    assert len(log.recent(limit=2)) == 2


def test_pagelog_ignores_a_none_page():
    log = PageLog()
    log.add(None)
    assert log.recent() == []


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
    return PocsagDecoder(spawn=lambda freq, device: FakePipeline(lines), **kw)


def test_decoder_collects_pages_from_the_pipeline():
    dec = _decoder([ALPHA, "noise", NUMERIC, TONE])
    dec.start()
    dec.drain()
    pages = dec.pages()
    assert len(pages) == 3
    assert pages[0]["kind"] == "tone"               # newest first
    assert {p["address"] for p in pages} == {1234567, 123456, 1000000}


def test_decoder_refuses_without_a_radio():
    dec = _decoder([], radio_present=lambda: False)
    with pytest.raises(PocsagUnavailable):
        dec.start()


def test_decoder_refuses_when_multimon_is_missing():
    dec = _decoder([], which=lambda name: None if name == "multimon-ng" else "/usr/bin/" + name)
    with pytest.raises(PocsagUnavailable):
        dec.start()


def test_decoder_refuses_once_released():
    dec = _decoder([ALPHA])
    dec.release()
    with pytest.raises(PocsagUnavailable):
        dec.start()
    dec.resume()
    dec.start()
    dec.drain()
    assert dec.pages()[0]["address"] == 1234567


def test_release_clears_the_log():
    dec = _decoder([ALPHA])
    dec.start()
    dec.drain()
    assert dec.pages()
    dec.release()
    assert dec.pages() == []


def test_a_reader_error_is_recorded_not_swallowed():
    class RaisingPipeline(FakePipeline):
        def __init__(self):
            def boom():
                raise OSError("pipe died")
                yield  # pragma: no cover
            self.stdout = boom()
            self.terminated = False

    dec = PocsagDecoder(spawn=lambda f, d: RaisingPipeline(), radio_present=lambda: True,
                        which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "pipe died"


def test_default_pocsag_frequency_is_in_the_us_pager_band():
    assert 928_000_000 <= DEFAULT_POCSAG_HZ <= 932_000_000
