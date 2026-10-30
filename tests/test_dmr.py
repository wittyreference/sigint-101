# ABOUTME: Tests the DMR/P25 backend — parsing dsd-fme signaling into call events, newest-first.
# ABOUTME: The dsd-fme process is a fake at the boundary; no radio / no dsd-fme is honest.

import pytest

from sigint101.dmr import (
    DEFAULT_DMR_HZ,
    CallLog,
    DmrDecoder,
    DmrUnavailable,
    parse_dsd_line,
)

# Real dsd-fme output lines. dsd-fme writes decoded signaling to stderr; printFrameSync() prints
# "<time> Sync: <frametype> " with no newline (src/dsd_frame_sync.c), and the protocol decoder then
# appends the call metadata to that same line.
#
# DMR voice call: a capture of dsd-fme decoding a Cap+ private call, quoted from the DSD-FME thread
# on RadioReference (forums.radioreference.com/threads/dsd-fme.438137, page 65). TGT/SRC and the
# active time-slot are on the one line.
DMR_CALL = (
    "16:00:15 Sync: +DMR  slot1 [slot2]  | Color Code=03 | VLC SLOT 2 "
    "TGT=21001 SRC=21002 FLCO=0x07 FID=0x10 SVC=0x00 Cap+ Private Call Cap+ R-Ch 3")

# DMR trunking control frame, quoted verbatim from the project's examples/Example_Usage.md. The
# grant header carries a sync and colour code but no call IDs on this line (they fall on the next
# line), so it is signaling, not a decodable call.
DMR_CSBK = "00:43:00 Sync: +DMR  [slot1]  slot2  | Color Code=00 | CSBK"

# P25 Phase 1 group voice call. Reconstructed from two verbatim dsd-fme format strings on one line:
# "Sync: +P25p1 " (src/dsd_frame_sync.c) followed by the LCW print " Group Voice Channel User" +
# " - Group %d Source %d" (src/p25_lcw.c, Group Voice Channel User LCW format 0x00).
P25_GROUP = "13:50:22 Sync: +P25p1  Group Voice Channel User - Group 2001 Source 1234567"

# P25 Phase 1 unit-to-unit (private) call, from the same source file's format 0x03 strings:
# " Unit to Unit Voice Channel User" + " - Target %d Source %d" (src/p25_lcw.c).
P25_PRIVATE = (
    "13:51:02 Sync: +P25p1  Unit to Unit Voice Channel User - Target 7654321 Source 1234567")

# A bare sync acquisition: printFrameSync() has fired but no call metadata was appended.
SYNC_ONLY = "13:50:20 Sync: +P25p1 "

# A mode dsd-fme can decode but this lesson does not cover: the sync is YSF, not DMR or P25.
YSF_SYNC = "14:02:11 Sync: +YSF  FICH OK"


def test_parse_a_dmr_voice_call():
    ev = parse_dsd_line(DMR_CALL)
    assert ev is not None
    assert ev.protocol == "DMR"
    assert ev.talkgroup == 21001
    assert ev.source == 21002
    assert ev.slot == 2
    assert ev.color_code == 3
    assert ev.kind == "private"
    assert ev.last_seen == "16:00:15"


def test_parse_a_p25_group_call():
    ev = parse_dsd_line(P25_GROUP)
    assert ev is not None
    assert ev.protocol == "P25"
    assert ev.talkgroup == 2001
    assert ev.source == 1234567
    assert ev.slot is None           # P25 Phase 1 has no DMR-style time-slot
    assert ev.color_code is None     # colour code is a DMR concept
    assert ev.kind == "group"
    assert ev.last_seen == "13:50:22"


def test_parse_a_p25_unit_to_unit_call():
    ev = parse_dsd_line(P25_PRIVATE)
    assert ev.protocol == "P25"
    assert ev.talkgroup == 7654321   # the called unit sits in the target field
    assert ev.source == 1234567
    assert ev.kind == "private"


def test_parse_as_dict_has_the_call_keys():
    ev = parse_dsd_line(DMR_CALL)
    assert ev.as_dict() == {
        "protocol": "DMR",
        "talkgroup": 21001,
        "source": 21002,
        "slot": 2,
        "color_code": 3,
        "kind": "private",
        "last_seen": "16:00:15",
    }


def test_parse_skips_a_sync_with_no_call():
    # Sync acquired but no talkgroup or source on the line — nothing to log, not a call.
    assert parse_dsd_line(SYNC_ONLY) is None
    assert parse_dsd_line(DMR_CSBK) is None


def test_parse_skips_an_unsupported_mode():
    assert parse_dsd_line(YSF_SYNC) is None


def test_parse_skips_non_dsd_lines():
    assert parse_dsd_line("") is None
    assert parse_dsd_line("Audio In Device: pulse, Audio Out Device: null") is None
    assert parse_dsd_line("no sync here") is None


def test_log_keeps_calls_newest_first():
    log = CallLog()
    log.add(parse_dsd_line(DMR_CALL))
    log.add(parse_dsd_line(P25_GROUP))
    calls = log.recent()
    assert len(calls) == 2
    assert calls[0].talkgroup == 2001        # newest first


def test_log_limit_bounds_the_history():
    log = CallLog(maxlen=2)
    for _ in range(5):
        log.add(parse_dsd_line(DMR_CALL))
    assert len(log.recent()) == 2


def test_log_recent_honours_a_limit_argument():
    log = CallLog()
    for _ in range(4):
        log.add(parse_dsd_line(P25_GROUP))
    assert len(log.recent(limit=2)) == 2


def test_log_ignores_a_none_event():
    log = CallLog()
    log.add(None)
    assert log.recent() == []


class FakeProcess:
    """Stands in for dsd-fme at the boundary (its decoded stderr stream, exposed as .stdout)."""

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
    return DmrDecoder(spawn=lambda freq, device: FakeProcess(lines), **kw)


def test_decoder_collects_calls_from_the_process():
    dec = _decoder([DMR_CALL, "noise", P25_GROUP, SYNC_ONLY])
    dec.start()
    dec.drain()
    calls = dec.calls()
    assert len(calls) == 2                    # the two real calls; noise and bare sync are dropped
    assert calls[0]["talkgroup"] == 2001      # newest first (the P25 group call)
    assert {c["protocol"] for c in calls} == {"DMR", "P25"}


def test_decoder_refuses_without_a_radio():
    dec = _decoder([], radio_present=lambda: False)
    with pytest.raises(DmrUnavailable):
        dec.start()


def test_decoder_refuses_when_dsd_fme_is_missing():
    dec = _decoder([], which=lambda name: None if name == "dsd-fme" else "/usr/bin/" + name)
    with pytest.raises(DmrUnavailable):
        dec.start()


def test_decoder_refuses_once_released():
    dec = _decoder([DMR_CALL])
    dec.release()
    with pytest.raises(DmrUnavailable):
        dec.start()
    assert dec.running is False               # released stays off the radio
    dec.resume()
    dec.start()
    dec.drain()
    assert dec.calls()[0]["source"] == 21002


def test_release_clears_the_log():
    dec = _decoder([DMR_CALL])
    dec.start()
    dec.drain()
    assert dec.calls()
    dec.release()
    assert dec.calls() == []


def test_a_reader_error_is_recorded_not_swallowed():
    class RaisingProcess(FakeProcess):
        def __init__(self):
            def boom():
                raise OSError("pipe died")
                yield  # pragma: no cover
            self.stdout = boom()
            self.terminated = False

    dec = DmrDecoder(spawn=lambda f, d: RaisingProcess(), radio_present=lambda: True,
                     which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "pipe died"


def test_default_dmr_frequency_is_uhf():
    assert DEFAULT_DMR_HZ == 450_000_000
