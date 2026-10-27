# ABOUTME: Tests the RDS backend — parsing redsea JSON groups and assembling the station view.
# ABOUTME: The rtl_fm|redsea pipeline is a fake at the boundary; no radio / no redsea is honest.

import pytest

from sigint101.rds import (
    DEFAULT_RDS_HZ,
    RDSDecoder,
    RDSStation,
    RDSUnavailable,
    parse_rds_group,
)

# Real redsea 1.3.1 output, captured by decoding redsea's own mpx-testfile-yksi.flac on nomad-b.
YKSI_0A = ('{"pi":"0x6201","group":"0A","tp":false,"prog_type":"Serious classical",'
           '"di":{"stereo":true},"ta":true,"is_music":true}')
YKSI_1A = '{"pi":"0x6201","group":"1A","tp":false,"prog_type":"Serious classical"}'
# redsea emits "ps" (the 8-char station name) and "radiotext" once enough groups assemble them;
# the short test file carries only one 0A, so these two use redsea's documented key shapes.
PS_LINE = '{"pi":"0x6201","group":"0A","ps":"YLEKLAS1"}'
RT_LINE = '{"pi":"0x6201","group":"2A","radiotext":"Now playing: Sibelius - Finlandia"}'


def test_parse_reads_a_real_redsea_group():
    g = parse_rds_group(YKSI_0A)
    assert g is not None
    assert g["pi"] == "0x6201"
    assert g["group"] == "0A"
    assert g["prog_type"] == "Serious classical"


def test_parse_skips_non_group_lines():
    assert parse_rds_group("") is None
    assert parse_rds_group("not json") is None
    assert parse_rds_group('{"version":"redsea"}') is None   # no group — not an RDS group


def test_station_assembles_the_hidden_data_across_groups():
    s = RDSStation()
    for line in (YKSI_0A, YKSI_1A, PS_LINE, RT_LINE):
        s.absorb(parse_rds_group(line))
    assert s.pi == "0x6201"
    assert s.prog_type == "Serious classical"
    assert s.ps == "YLEKLAS1"
    assert s.radiotext == "Now playing: Sibelius - Finlandia"
    assert s.group_count == 4


def test_station_keeps_the_latest_text():
    s = RDSStation()
    s.absorb(parse_rds_group(PS_LINE))
    s.absorb(parse_rds_group('{"pi":"0x6201","group":"0A","ps":"YLEKLAS2"}'))
    assert s.ps == "YLEKLAS2"


def test_station_as_dict_carries_what_the_api_sends():
    s = RDSStation()
    s.absorb(parse_rds_group(YKSI_0A))
    s.absorb(parse_rds_group(PS_LINE))
    out = s.as_dict()
    assert out["pi"] == "0x6201"
    assert out["ps"] == "YLEKLAS1"
    assert out["prog_type"] == "Serious classical"
    assert out["group_count"] == 2


class FakePipeline:
    """Stands in for the rtl_fm|redsea pipeline at the process boundary."""

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
    return RDSDecoder(spawn=lambda freq, device: FakePipeline(lines), **kw)


def test_decoder_assembles_the_station_from_the_pipeline():
    dec = _decoder([YKSI_0A, "garbage", YKSI_1A, PS_LINE, RT_LINE])
    dec.start()
    dec.drain()
    s = dec.station()
    assert s.ps == "YLEKLAS1"
    assert s.radiotext == "Now playing: Sibelius - Finlandia"
    assert s.group_count == 4


def test_decoder_refuses_without_a_radio():
    dec = _decoder([], radio_present=lambda: False)
    with pytest.raises(RDSUnavailable):
        dec.start()


def test_decoder_refuses_when_redsea_is_missing():
    dec = _decoder([], which=lambda name: None if name == "redsea" else "/usr/bin/" + name)
    with pytest.raises(RDSUnavailable):
        dec.start()


def test_decoder_refuses_once_released():
    dec = _decoder([YKSI_0A])
    dec.release()
    with pytest.raises(RDSUnavailable):
        dec.start()
    dec.resume()
    dec.start()
    dec.drain()
    assert dec.station().pi == "0x6201"


def test_default_rds_frequency_is_an_fm_station():
    assert 88_000_000 <= DEFAULT_RDS_HZ <= 108_000_000


def test_station_ignores_a_none_group():
    s = RDSStation()
    s.absorb(None)
    s.absorb(parse_rds_group("not a group"))
    assert s.group_count == 0 and s.pi is None


def test_stop_reaps_the_pipeline_and_releases_the_dongle():
    box = {}

    def spawn(freq, device):
        box["p"] = FakePipeline([YKSI_0A])
        return box["p"]

    dec = RDSDecoder(spawn=spawn, radio_present=lambda: True,
                     which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    dec.stop()
    assert box["p"].terminated is True
    assert dec.running is False


def test_a_reader_error_is_recorded_not_swallowed():
    class RaisingPipeline(FakePipeline):
        def __init__(self):
            def boom():
                raise OSError("pipe died")
                yield  # pragma: no cover
            self.stdout = boom()
            self.terminated = False

    dec = RDSDecoder(spawn=lambda f, d: RaisingPipeline(), radio_present=lambda: True,
                     which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "pipe died"


def test_a_device_it_cannot_single_out_is_refused():
    from sigint101.hardware import RtlSdrDevice
    dec = RDSDecoder(spawn=lambda f, d: FakePipeline([]), radio_present=lambda: True,
                     which=lambda name: "/usr/bin/" + name,
                     list_devices=lambda: (RtlSdrDevice(0, "00000001"), RtlSdrDevice(1, "1004")))
    with pytest.raises(RDSUnavailable):
        dec.start()


def test_pipeline_terminates_and_waits_both_processes():
    from sigint101.rds import _Pipeline

    class P:
        def __init__(self):
            self.terminated = False
            self.waited = False
            self.stdout = iter([])

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True

        def poll(self):
            return 7

    rtl, red = P(), P()
    pipe = _Pipeline(rtl, red)
    assert pipe.poll() == 7
    pipe.terminate()
    pipe.wait(timeout=1)
    assert rtl.terminated and red.terminated
    assert rtl.waited and red.waited
