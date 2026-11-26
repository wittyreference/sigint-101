# ABOUTME: Tests operator mode's own rules: the rack's instruments, each decoder's band limits,
# ABOUTME: and the provenance-stamped reception record a live decode is logged as.

import copy
import json

import pytest

from sigint101.operate import (
    DEFAULT_INSTRUMENT,
    INSTRUMENTS,
    command_gain,
    instrument,
    reception_record,
    reception_summary,
    tunable,
    validate_decoder_hz,
    without_page_words,
)
from sigint101.spectrum import TuneError

# --- the rack's instruments --------------------------------------------------------------------


def test_the_rack_offers_the_instruments_the_spec_lists_in_its_order():
    assert [i.label for i in INSTRUMENTS] == [
        "Waterfall", "ISM", "ADS-B", "APRS", "Pager framing", "ACARS", "AIS", "RDS", "DMR/P25",
        "APT", "SSTV", "LoRa", "Flock", "Rayhunter", "Flipper", "Clock", "Health"]


def test_every_instrument_has_a_distinct_url_key_and_the_waterfall_is_the_default():
    keys = [i.key for i in INSTRUMENTS]
    assert len(set(keys)) == len(keys)
    assert DEFAULT_INSTRUMENT == "waterfall"
    assert instrument("waterfall").key == "waterfall"
    assert instrument("no-such-thing") is None


def test_only_the_single_channel_decoders_take_a_frequency_override():
    assert sorted(i.api for i in INSTRUMENTS if i.band) == [
        "acars", "aprs", "decode", "dmr", "pocsag", "rds"]
    assert tunable("aprs").key == "aprs"
    # Fixed-channel and non-SDR instruments have no override: ADS-B is 1090 by definition, AIS
    # decodes both marine channels at once, and the waterfall tunes through /api/tune.
    for api in ("adsb", "ais", "apt", "sstv", "lora", "flock", "rayhunter", "flipper", "spectrum",
                "nope"):
        assert tunable(api) is None


def test_the_radios_each_instrument_rides():
    sdr = {i.key for i in INSTRUMENTS if i.radio == "rtl-sdr"}
    assert {"lora", "flock", "rayhunter", "flipper"}.isdisjoint(sdr)
    assert {"waterfall", "ism", "aprs", "pager", "clock", "health"} <= sdr


# --- band sanity limits -------------------------------------------------------------------------


@pytest.mark.parametrize("api, inside, outside", [
    ("decode", 315_000_000, 1_090_000_000),     # rtl_433 sensors: 300-928 MHz
    ("aprs", 144_800_000, 150_000_000),         # the 2 m band
    ("pocsag", 152_007_500, 100_000_000),       # VHF, UHF and 900 MHz paging
    ("rds", 98_100_000, 110_000_000),           # FM broadcast
    ("acars", 131_725_000, 140_000_000),        # the VHF air band
    ("dmr", 462_562_500, 120_000_000),          # land-mobile radio
])
def test_each_decoder_accepts_its_band_and_refuses_outside_it_with_the_reason(api, inside,
                                                                              outside):
    inst = tunable(api)
    assert validate_decoder_hz(inst, inside) == inside
    with pytest.raises(TuneError) as caught:
        validate_decoder_hz(inst, outside)
    assert f"{inst.band.low_hz / 1e6:g}" in str(caught.value)
    assert f"{inst.band.high_hz / 1e6:g}" in str(caught.value)


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan"), float("inf"), True, [1]])
def test_an_unreadable_frequency_is_refused_not_guessed(bad):
    with pytest.raises(TuneError):
        validate_decoder_hz(tunable("aprs"), bad)


def test_a_frequency_given_as_a_numeric_string_is_read():
    assert validate_decoder_hz(tunable("aprs"), "144390000") == 144_390_000


def test_the_paging_band_stays_tunable_and_its_text_rule_is_the_decoders():
    # The rack may tune into 929-932 MHz and anywhere else in the pager's range; what it shows
    # is the decoder's business (framing only, on every frequency), not a lockout here.
    assert validate_decoder_hz(tunable("pocsag"), 929_612_500) == 929_612_500
    assert validate_decoder_hz(tunable("pocsag"), 152_840_000) == 152_840_000


def test_every_tunable_instrument_names_its_steps():
    for inst in INSTRUMENTS:
        if inst.band or inst.api == "spectrum":
            assert inst.steps and all(s > 0 for s in inst.steps)
        else:
            assert inst.steps == ()


# --- the gain a live command sets --------------------------------------------------------------


@pytest.mark.parametrize("command, gain", [
    ("rtl_fm -M fm -f 144390000 -s 22050 -g 49.6 -l 0 -d 0 | multimon-ng -t raw -a AFSK1200 -",
     "49.6"),
    ("rtl_power -f 88000000:108000000:125000 -i 1 -d 0 -g 28.0", "28.0"),
    ("satdump live noaa_apt /tmp --source rtlsdr --gain 49", "49"),
    ("satdump live noaa_apt /tmp --source rtlsdr --general_gain 49.6", None),
    ("dsd-fme -i rtl:0:450000000:0:0:12 -fa", "0"),
    ("rtl_433 -f 433920000 -F json -M level -d 0", None),
    (None, None),
])
def test_the_gain_is_read_off_the_command_line_as_written(command, gain):
    assert command_gain(command) == gain


# --- what a reception line says -----------------------------------------------------------------


def test_an_ism_reception_reads_the_sensor_and_its_reading():
    line = reception_summary("ism", {"model": "Acurite-Tower", "mod": "ASK",
                                     "readings": {"id": 1, "temperature_C": 18.0,
                                                  "humidity": 51}})
    assert line == "Acurite-Tower · 18.0 °C · 51% RH"


def test_a_pager_reception_carries_framing_only():
    page = {"baud": 1200, "address": 1234567, "function": 3, "kind": "alpha", "text": None,
            "length": 20, "text_withheld": True}
    line = reception_summary("pager", page)
    assert "1234567" in line and "20" in line and "withheld" in line


def test_a_pager_reception_line_has_no_place_for_a_pages_words():
    # The decoder never hands the words over; if an item ever carried some, the line would still
    # not print them.
    page = {"baud": 1200, "address": 1234567, "function": 3, "kind": "alpha",
            "text": "CALL EXT 2210 URGENT", "length": 20, "text_withheld": False}
    line = reception_summary("pager", page)
    assert "CALL EXT" not in line
    assert "1234567" in line and "20" in line


@pytest.mark.parametrize("key, item, expect", [
    ("aprs", {"source": "KD2ABC-9", "kind": "position", "lat": 40.7, "lon": -74.0,
              "text": "hi"}, "KD2ABC-9"),
    ("rds", {"pi": "0x6201", "ps": "YLEKLAS1"}, "YLEKLAS1"),
    ("acars", {"flight": "AF7728", "tail": "F-GTAE", "label": "H1"}, "AF7728"),
    ("ais", {"mmsi": 477553000, "name": "OCEAN"}, "477553000"),
    ("adsb", {"hex_ident": "a1b2c3", "callsign": None}, "a1b2c3"),
    ("dmr", {"protocol": "DMR", "talkgroup": 21001, "source": 21002}, "21001"),
    ("lora", {"from_id": "!abcd", "snr_db": -7.5, "summary": "hello"}, "!abcd"),
    ("flock", {"mac": "aa:bb:cc:dd:ee:ff", "tier": "high"}, "aa:bb:cc:dd:ee:ff"),
    ("rayhunter", {"name": "IMSI Requested", "severity": "low"}, "IMSI Requested"),
    ("apt", {"path": "apt/x.png", "meta": {"filename": "x.png"}}, "x.png"),
    ("sstv", {"path": "sstv/y.png", "mode": "PD120", "meta": {}}, "PD120"),
])
def test_every_instrument_has_a_one_line_summary(key, item, expect):
    assert expect in reception_summary(key, item)


def test_a_summary_never_fails_on_a_sparse_item():
    for inst in INSTRUMENTS:
        assert isinstance(reception_summary(inst.key, {}), str)


def test_a_reception_record_carries_its_provenance():
    record = reception_record(
        instrument("aprs"), {"source": "KD2ABC-9", "kind": "status", "text": "hi"},
        freq_hz=144_390_000,
        command="rtl_fm -M fm -f 144390000 -g 49.6 -d 0 | multimon-ng -t raw -a AFSK1200 -",
        ppm={"correction": "raw", "measured_ppm": None},
        radio={"kind": "rtl-sdr", "serial": "00000001", "index": 0})
    assert record["instrument"] == "aprs"
    assert record["freq_hz"] == 144_390_000
    assert record["gain"] == "49.6"
    assert record["ppm"] == {"correction": "raw", "measured_ppm": None}
    assert record["radio"]["serial"] == "00000001"
    assert record["command"].startswith("rtl_fm")
    assert record["summary"].startswith("KD2ABC-9")
    assert record["item"]["source"] == "KD2ABC-9"


def test_a_reception_record_names_the_tool_its_receiver_runs_when_it_has_its_own():
    # The Flock instrument's tool is the configured detector's own: Marauder on the Flipper's Wi-Fi
    # devboard, typed `sniffprobe -serial`, is never logged as the default source's tcpdump.
    ppm = {"correction": "raw", "measured_ppm": None}
    record = reception_record(
        instrument("flock"), {"mac": "b4:1e:52:00:00:01", "tier": 4},
        freq_hz=None, command="sniffprobe -serial", ppm=ppm,
        radio={"kind": "wifi", "interface": "/dev/ttyACM1"}, tool="Marauder")
    assert record["tool"] == "Marauder"
    assert record["command"] == "sniffprobe -serial"
    # A receiver with no tool of its own is logged under its instrument's.
    plain = reception_record(instrument("aprs"), {"source": "KD2ABC-9"}, freq_hz=144_390_000,
                             command=None, ppm=ppm, radio=None)
    assert plain["tool"] == "multimon-ng"


# --- a stored pager reception is read in without its page's words -----------------------------

PAGER_COMMAND = ("rtl_fm -M fm -f 152840000 -s 22050 -g 49.6 -l 0 -d 0 | "
                 "multimon-ng -t raw -a POCSAG512 -a POCSAG1200 -a POCSAG2400 -")

# A pager reception as an event log can hold it: the item carries the page's words, and so does
# the line that was built from that item.
STORED_PAGE = {
    "ts": 1794800000.0,
    "kind": "reception",
    "payload": {
        "instrument": "pager", "tool": "multimon-ng", "freq_hz": 152_840_000, "gain": "49.6",
        "ppm": {"correction": "raw", "measured_ppm": None},
        "radio": {"kind": "rtl-sdr", "serial": None, "index": 0},
        "command": PAGER_COMMAND,
        "summary": "cap 1234567 · alpha · 20 chars · CALL EXT 2210 URGENT",
        "item": {"baud": 1200, "address": 1234567, "function": 3, "kind": "alpha",
                 "text": "CALL EXT 2210 URGENT", "length": 20, "text_withheld": False},
    },
}


def test_a_stored_pager_reception_is_read_without_its_pages_words():
    stored = copy.deepcopy(STORED_PAGE)
    served = without_page_words(stored)
    assert "CALL EXT" not in json.dumps(served)
    payload = served["payload"]
    assert payload["item"] == {"baud": 1200, "address": 1234567, "function": 3, "kind": "alpha",
                               "text": None, "length": 20, "text_withheld": True}
    assert payload["summary"] == "cap 1234567 · alpha · 20 chars · text withheld"
    # The provenance is read as it was logged.
    for key in ("instrument", "tool", "freq_hz", "gain", "ppm", "radio", "command"):
        assert payload[key] == STORED_PAGE["payload"][key]
    assert (served["ts"], served["kind"]) == (STORED_PAGE["ts"], "reception")
    # The stored event itself is copied, never changed.
    assert stored == STORED_PAGE


def test_a_pager_reception_stored_without_words_reads_back_the_same():
    item = {"baud": 1200, "address": 1234567, "function": 3, "kind": "alpha", "text": None,
            "length": 20, "text_withheld": True}
    record = reception_record(instrument("pager"), item, freq_hz=929_612_500,
                              command=PAGER_COMMAND, ppm={"correction": "raw"},
                              radio={"kind": "rtl-sdr", "serial": None, "index": 0})
    event = {"ts": 1.0, "kind": "reception", "payload": record}
    assert without_page_words(copy.deepcopy(event)) == event


def test_only_a_pager_reception_is_read_differently_from_how_it_was_logged():
    # An APRS status is the station's own public text: it is read exactly as it was logged.
    aprs = {"ts": 1.0, "kind": "reception",
            "payload": {"instrument": "aprs", "summary": "KD2ABC-9 · hi",
                        "item": {"source": "KD2ABC-9", "kind": "status", "text": "hi"}}}
    assert without_page_words(aprs) is aprs


@pytest.mark.parametrize("payload", [
    None,
    {"instrument": "pager"},
    {"instrument": "pager", "item": None, "summary": "cap 1 · alpha · CALL EXT 2210 URGENT"},
    {"instrument": "pager", "item": "CALL EXT 2210 URGENT"},
    {"instrument": "pager", "item": {"text": "CALL EXT 2210 URGENT"}},
])
def test_a_damaged_stored_reception_is_read_without_failing_or_leaking(payload):
    served = without_page_words({"ts": 1.0, "kind": "reception", "payload": payload})
    assert "CALL EXT" not in json.dumps(served)
