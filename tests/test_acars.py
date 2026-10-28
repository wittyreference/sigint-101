# ABOUTME: Tests the ACARS backend — parsing acarsdec JSON into aircraft messages, newest-first.
# ABOUTME: The acarsdec process is a fake at the boundary; no radio / no acarsdec is honest.

import pytest

from sigint101.acars import (
    DEFAULT_ACARS_HZ,
    AcarsDecoder,
    AcarsLog,
    AcarsUnavailable,
    parse_acars_message,
)

# Real acarsdec 3.7 '-o 4' JSON lines, captured by decoding acarsdec's own test.wav on nomad-b.
# One message carries free text (an H1 report from AF7728); the link-test/ack messages carry none.
WITH_TEXT = (
    '{"timestamp":1793151825.5348461,"station_id":"spec5b","channel":0,"freq":131.550,'
    '"level":-8.5,"error":0,"mode":"G","label":"H1","block_id":"3","ack":false,'
    '"tail":"F-GTAE","flight":"AF7728","msgno":"D65C",'
    '"text":"#DFB00000/V206,05,124,183,02,00,00000/V7044,078,00081,22222222222111/",'
    '"app":{"name":"acarsdec","ver":"3.7"}}')
NO_TEXT = (
    '{"timestamp":1793151825.53618,"station_id":"spec5b","channel":1,"freq":131.725,'
    '"level":-23.0,"error":0,"mode":"E","label":"5V","block_id":"4","ack":false,'
    '"tail":"PH-BXR","flight":"KL1681","msgno":"S53A","app":{"name":"acarsdec","ver":"3.7"}}')
LINK_TEST = (
    '{"timestamp":1793151825.5393629,"station_id":"spec5b","channel":3,"freq":131.825,'
    '"level":-17.4,"error":0,"mode":"2","label":"Q0","block_id":"4","ack":false,'
    '"tail":"LN-DYY","flight":"DY083J","msgno":"S46A","app":{"name":"acarsdec","ver":"3.7"}}')


def test_parse_a_message_with_text():
    m = parse_acars_message(WITH_TEXT)
    assert m is not None
    assert m.tail == "F-GTAE"
    assert m.flight == "AF7728"
    assert m.label == "H1"
    assert m.mode == "G"
    assert m.block_id == "3"
    assert m.text.startswith("#DFB00000")


def test_parse_a_message_with_no_text():
    m = parse_acars_message(NO_TEXT)
    assert m.tail == "PH-BXR"
    assert m.flight == "KL1681"
    assert m.label == "5V"
    assert m.text is None            # a link-test/ack message carries no free text


def test_parse_reads_the_frequency_and_level():
    m = parse_acars_message(LINK_TEST)
    assert m.freq_mhz == pytest.approx(131.825)
    assert m.level == pytest.approx(-17.4)


def test_parse_skips_non_acars_lines():
    assert parse_acars_message("") is None
    assert parse_acars_message("not json") is None
    assert parse_acars_message('{"app":{"name":"acarsdec"}}') is None   # no label — not a message
    assert parse_acars_message("[1,2,3]") is None                        # not an object


def test_log_keeps_messages_newest_first():
    log = AcarsLog()
    log.add(parse_acars_message(NO_TEXT))
    log.add(parse_acars_message(WITH_TEXT))
    msgs = log.recent()
    assert len(msgs) == 2
    assert msgs[0].flight == "AF7728"        # newest first


def test_log_limit_bounds_the_history():
    log = AcarsLog(maxlen=2)
    for _ in range(5):
        log.add(parse_acars_message(WITH_TEXT))
    assert len(log.recent()) == 2


def test_log_recent_honours_a_limit_argument():
    log = AcarsLog()
    for _ in range(4):
        log.add(parse_acars_message(WITH_TEXT))
    assert len(log.recent(limit=2)) == 2


def test_log_ignores_a_none_message():
    log = AcarsLog()
    log.add(None)
    assert log.recent() == []


class FakeProcess:
    """Stands in for the acarsdec process at the boundary (its stdout is a line iterator)."""

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
    return AcarsDecoder(spawn=lambda freq, device: FakeProcess(lines), **kw)


def test_decoder_collects_messages_from_the_process():
    dec = _decoder([NO_TEXT, "noise", WITH_TEXT, LINK_TEST])
    dec.start()
    dec.drain()
    msgs = dec.messages()
    assert len(msgs) == 3
    assert msgs[0]["flight"] == "DY083J"              # newest first
    assert {m["tail"] for m in msgs} == {"F-GTAE", "PH-BXR", "LN-DYY"}


def test_decoder_refuses_without_a_radio():
    dec = _decoder([], radio_present=lambda: False)
    with pytest.raises(AcarsUnavailable):
        dec.start()


def test_decoder_refuses_when_acarsdec_is_missing():
    dec = _decoder([], which=lambda name: None if name == "acarsdec" else "/usr/bin/" + name)
    with pytest.raises(AcarsUnavailable):
        dec.start()


def test_decoder_refuses_once_released():
    dec = _decoder([WITH_TEXT])
    dec.release()
    with pytest.raises(AcarsUnavailable):
        dec.start()
    dec.resume()
    dec.start()
    dec.drain()
    assert dec.messages()[0]["tail"] == "F-GTAE"


def test_release_clears_the_log():
    dec = _decoder([WITH_TEXT])
    dec.start()
    dec.drain()
    assert dec.messages()
    dec.release()
    assert dec.messages() == []


def test_a_reader_error_is_recorded_not_swallowed():
    class RaisingProcess(FakeProcess):
        def __init__(self):
            def boom():
                raise OSError("pipe died")
                yield  # pragma: no cover
            self.stdout = boom()
            self.terminated = False

    dec = AcarsDecoder(spawn=lambda f, d: RaisingProcess(), radio_present=lambda: True,
                       which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "pipe died"


def test_default_acars_frequency_is_the_primary_vhf_channel():
    assert DEFAULT_ACARS_HZ == 131_550_000
