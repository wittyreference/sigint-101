# ABOUTME: Tests the POCSAG backend — multimon-ng pager lines parsed into framing, text withheld on
# ABOUTME: the commercial paging band. The pipeline is a fake at the boundary; no radio is honest.

import pytest

from sigint101.pocsag import (
    DEFAULT_POCSAG_HZ,
    PageLog,
    PocsagDecoder,
    PocsagUnavailable,
    on_commercial_paging_band,
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
    p = parse_pocsag_line(ALPHA, keep_text=True)
    assert p is not None
    assert p.baud == 1200
    assert p.address == 1234567
    assert p.function == 3
    assert p.kind == "alpha"
    assert p.text == "CALL EXT 2210 URGENT"


def test_parse_a_numeric_page():
    p = parse_pocsag_line(NUMERIC, keep_text=True)
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
    p = parse_pocsag_line(NO_ADDR, keep_text=True)
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
    assert pages[0].kind == "alpha"   # newest first


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


def test_default_pocsag_frequency_is_an_assigned_paging_channel():
    # 47 CFR 90.493(a): 929-930 MHz paging channels run 929.0125 + n x 25 kHz. 929.6000 sat
    # between two of them (technical audit T-12), so a pager on either landed 12.5 kHz off.
    assert 929_000_000 < DEFAULT_POCSAG_HZ < 930_000_000
    assert (DEFAULT_POCSAG_HZ - 929_012_500) % 25_000 == 0


# --- framing live, text hidden (M15, MC's call 2026-11-02) --------------------
# Pages on the commercial paging band are carried by a paging company, so under 18 USC
# 2510(16)(D) they are not "readily accessible to the general public" and reading the words is
# interception (2511(1)(a)). The panel shows the framing — cap code, function, kind, baud and
# message length — and the words are dropped at parse time, so they are never stored, logged or
# served.

SECRET = "CALL EXT 2210 URGENT"


def test_the_commercial_paging_band_is_929_to_932_mhz():
    assert on_commercial_paging_band(929_012_500)
    assert on_commercial_paging_band(DEFAULT_POCSAG_HZ)
    assert on_commercial_paging_band(931_987_500)
    assert not on_commercial_paging_band(928_999_999)
    assert not on_commercial_paging_band(932_000_001)
    assert not on_commercial_paging_band(439_987_500)   # amateur POCSAG (DAPNET)


def test_parsing_withholds_the_text_by_default_and_keeps_its_length():
    p = parse_pocsag_line(ALPHA)
    assert p.text is None
    assert p.text_withheld is True
    assert p.length == len(SECRET)
    assert (p.baud, p.address, p.function, p.kind) == (1200, 1234567, 3, "alpha")


def test_a_withheld_numeric_page_keeps_its_digit_count():
    p = parse_pocsag_line(NUMERIC)
    assert p.text is None
    assert p.length == 10


def test_a_tone_only_page_has_nothing_to_withhold():
    p = parse_pocsag_line(TONE)
    assert p.text is None
    assert p.text_withheld is False
    assert p.length == 0


def test_a_withheld_page_dict_never_carries_the_words():
    page = parse_pocsag_line(ALPHA).as_dict()
    assert page["text"] is None
    assert page["text_withheld"] is True
    assert page["length"] == len(SECRET)
    assert SECRET not in repr(page)


def test_a_kept_page_reports_its_text_and_length():
    page = parse_pocsag_line(ALPHA, keep_text=True).as_dict()
    assert page["text"] == SECRET
    assert page["text_withheld"] is False
    assert page["length"] == len(SECRET)


def test_the_decoder_never_stores_text_on_the_commercial_band():
    dec = _decoder([ALPHA, NUMERIC])
    assert dec.freq_hz == DEFAULT_POCSAG_HZ
    dec.start()
    dec.drain()
    pages = dec.pages()
    assert [p["text"] for p in pages] == [None, None]
    assert all(p["text_withheld"] for p in pages)
    assert SECRET not in repr(dec._log.recent())


def test_the_decoder_keeps_text_off_the_commercial_band():
    dec = _decoder([ALPHA], freq_hz=439_987_500)
    dec.start()
    dec.drain()
    assert dec.pages()[0]["text"] == SECRET
