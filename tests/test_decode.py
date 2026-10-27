# ABOUTME: Tests the rtl_433 decode backend — JSON-line parsing and the Decoder lifecycle.
# ABOUTME: The process is a fake at the boundary; no radio or no rtl_433 is an honest refusal.

import pytest

from sigint101.decode import (
    DEFAULT_DECODE_HZ,
    Decoder,
    DecodeUnavailable,
    parse_decode_line,
)

# Real rtl_433 -F json output shapes. The ASK line is one captured live on nomad-b from an
# Acurite-Tower weather sensor; the FSK line is rtl_433's documented FSK shape, which reports
# the two mark/space tones as freq1 and freq2 (the whole point of M10) rather than one freq.
ASK_LINE = ('{"time" : "2026-10-27 13:04:11", "model" : "Acurite-Tower", "id" : 16262, '
            '"channel" : "A", "battery_ok" : 1, "temperature_C" : 18.000, "humidity" : 51, '
            '"mic" : "CHECKSUM", "mod" : "ASK", "freq" : 433.950, "rssi" : -1.112, '
            '"snr" : 36.261, "noise" : -37.373}')
FSK_LINE = ('{"time" : "2026-10-27 13:10:02", "model" : "LaCrosse-TX141THBv2", "id" : 252, '
            '"channel" : 0, "battery_ok" : 1, "temperature_C" : 21.3, "humidity" : 45, '
            '"mod" : "FSK", "freq1" : 433.882, "freq2" : 433.958, "rssi" : -8.1, '
            '"snr" : 22.4, "noise" : -30.5}')


def test_parse_reads_an_ask_sensor_line():
    d = parse_decode_line(ASK_LINE)
    assert d is not None
    assert d.model == "Acurite-Tower"
    assert d.mod == "ASK"
    assert d.freq_hz == 433_950_000
    assert d.freq2_hz is None            # ASK is one frequency switched on and off
    assert d.snr_db == pytest.approx(36.261)
    assert d.readings["temperature_C"] == pytest.approx(18.0)
    assert d.readings["humidity"] == 51
    # the envelope keys are not sensor readings
    assert "freq" not in d.readings and "snr" not in d.readings and "model" not in d.readings


def test_parse_reads_an_fsk_sensor_with_both_tones():
    d = parse_decode_line(FSK_LINE)
    assert d is not None
    assert d.model == "LaCrosse-TX141THBv2"
    assert d.mod == "FSK"
    # FSK rides two frequencies; both are carried so the lesson can name the shift.
    assert d.freq_hz == 433_882_000
    assert d.freq2_hz == 433_958_000
    assert d.readings["temperature_C"] == pytest.approx(21.3)


def test_parse_skips_a_non_json_narration_line():
    assert parse_decode_line("Tuned to 433.920MHz.") is None
    assert parse_decode_line("") is None
    assert parse_decode_line("   ") is None


def test_parse_skips_json_without_a_model():
    # rtl_433 can emit non-decode JSON (e.g. a stats frame); it is not a sensor read.
    assert parse_decode_line('{"time" : "2026-10-27 13:00:00", "count" : 3}') is None


def test_as_dict_round_trips_the_fields_the_api_sends():
    d = parse_decode_line(FSK_LINE)
    out = d.as_dict()
    assert out["model"] == "LaCrosse-TX141THBv2"
    assert out["mod"] == "FSK"
    assert out["freq_hz"] == 433_882_000
    assert out["freq2_hz"] == 433_958_000
    assert out["readings"]["humidity"] == 45


class FakeProc:
    """Stands in for a Popen'd rtl_433 at the process boundary."""

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
    return Decoder(spawn=lambda cmd: FakeProc(lines), **kw)


def test_decoder_buffers_parsed_reads():
    dec = _decoder([ASK_LINE, "Tuned to 433.920MHz.", FSK_LINE])
    dec.start()
    dec.drain()
    reads = dec.recent()
    assert [r.model for r in reads] == ["Acurite-Tower", "LaCrosse-TX141THBv2"]


def test_decoder_buffer_is_bounded_to_newest():
    lines = [ASK_LINE.replace("16262", str(1000 + n)) for n in range(30)]
    dec = _decoder(lines, history=5)
    dec.start()
    dec.drain()
    reads = dec.recent()
    assert len(reads) == 5
    assert reads[-1].readings["id"] == 1029


def test_decoder_refuses_without_a_radio():
    dec = _decoder([], radio_present=lambda: False)
    with pytest.raises(DecodeUnavailable):
        dec.start()


def test_decoder_refuses_when_rtl_433_is_not_installed():
    dec = _decoder([], which=lambda name: None)
    with pytest.raises(DecodeUnavailable):
        dec.start()


def test_decoder_refuses_to_start_once_released():
    dec = _decoder([ASK_LINE])
    dec.release()
    with pytest.raises(DecodeUnavailable):
        dec.start()
    dec.resume()
    dec.start()          # taking the radio back works
    dec.drain()
    assert dec.recent()


def test_command_targets_the_decode_frequency_and_json():
    dec = Decoder(freq_hz=433_920_000, device=0)
    cmd = dec.command()
    assert "rtl_433" in cmd[0]
    assert "433920000" in cmd
    assert "json" in cmd


def test_default_decode_frequency_is_the_ism_band():
    assert DEFAULT_DECODE_HZ == 433_920_000


def test_recent_keeps_only_the_newest_when_limited():
    dec = _decoder([ASK_LINE.replace("16262", str(1000 + n)) for n in range(6)])
    dec.start()
    dec.drain()
    assert [r.readings["id"] for r in dec.recent(2)] == [1004, 1005]


def test_stop_reaps_the_process_and_releases_the_dongle():
    proc_box = {}

    def spawn(cmd):
        proc_box["proc"] = FakeProc([ASK_LINE])
        return proc_box["proc"]

    dec = Decoder(spawn=spawn, radio_present=lambda: True,
                  which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    dec.stop()
    assert proc_box["proc"].terminated is True
    assert dec.running is False


def test_a_reader_error_is_recorded_loudly_not_swallowed():
    class RaisingProc(FakeProc):
        def __init__(self):
            def boom():
                raise OSError("pipe died")
                yield  # pragma: no cover - makes this a generator
            self.stdout = boom()
            self.terminated = False

    dec = Decoder(spawn=lambda cmd: RaisingProc(), radio_present=lambda: True,
                  which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "pipe died"


def test_a_device_it_cannot_single_out_is_a_decode_refusal():
    # Two radios on the bus and none pinned: the decoder refuses rather than opening an arbitrary
    # tuner, exactly as the sweeper does — a wrong-radio decode would look perfectly real.
    from sigint101.hardware import RtlSdrDevice
    dec = Decoder(spawn=lambda cmd: FakeProc([]), radio_present=lambda: True,
                  which=lambda name: "/usr/bin/" + name,
                  list_devices=lambda: (RtlSdrDevice(0, "00000001"), RtlSdrDevice(1, "1004")))
    with pytest.raises(DecodeUnavailable):
        dec.start()


def test_parse_tolerates_a_malformed_frequency_without_inventing_one():
    # A line whose freq is not a number still parses (it is a real read), but freq_hz is 0 rather
    # than a guessed frequency, and a non-numeric snr becomes None.
    d = parse_decode_line(
        '{"time":"t","model":"X","mod":"ASK","freq":"nope","snr":"bad","humidity":5}')
    assert d is not None
    assert d.freq_hz == 0 and d.snr_db is None
    assert d.readings["humidity"] == 5
