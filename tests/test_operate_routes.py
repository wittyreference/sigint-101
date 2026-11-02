# ABOUTME: Integration tests for operator mode's routes: the rack pages, the target-centred sweep
# ABOUTME: tune, the decoder frequency overrides with their band limits, and the reception log.

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101 import childproc
from sigint101.app import create_app
from sigint101.aprs import AprsDecoder
from sigint101.decode import Decoder
from sigint101.events import read_events
from sigint101.hardware import RadioProbe
from sigint101.operate import INSTRUMENTS
from sigint101.pocsag import PocsagDecoder
from sigint101.spectrum import DEFAULT_HIGH, DEFAULT_LOW, Sweeper, target_span
from tests.child_fakes import Child

FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"

APRS_POS = "APRS: KD2ABC-9>APU25N,WIDE1-1,WIDE2-1:!4042.00N/07400.00W>Test de KD2ABC"
POCSAG_ALPHA = "POCSAG1200: Address: 1234567  Function: 3  Alpha:   CALL EXT 2210 URGENT"
RTL433_ASK = ('{"time" : "2026-10-27 13:04:11", "model" : "Acurite-Tower", "id" : 16262, '
              '"channel" : "A", "battery_ok" : 1, "temperature_C" : 18.000, "humidity" : 51, '
              '"mod" : "ASK", "freq" : 433.920, "snr" : 36.261}')


def _present():
    return RadioProbe(True, "lsusb", "RTL2832U 0bda:2838 on USB bus")


def _absent():
    return RadioProbe(False, "lsusb", "no RTL2832U on USB bus")


def _tools():
    return {"radio_present": lambda: True, "which": lambda name: "/usr/bin/" + name}


@pytest.fixture(autouse=True)
def _quick_stops(monkeypatch):
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0.01)
    monkeypatch.delenv("SIGINT101_RTLSDR_SERIAL", raising=False)


def _client(tmp_path, *, probe=_absent, pack_dir=FIXTURE_PACKS, **factories):
    return TestClient(create_app(pack_dir=pack_dir, data_dir=tmp_path, probe=probe,
                                 **factories))


def _events(tmp_path, kind):
    return [e for e in read_events(tmp_path / "events.jsonl") if e["kind"] == kind]


def _sweeper():
    return Sweeper(spawn=lambda cmd: Child(cmd), radio_present=lambda: True)


# --- the rack's pages ---------------------------------------------------------------------------


def test_operate_opens_the_waterfall_until_another_instrument_is_used(tmp_path):
    with _client(tmp_path) as client:
        first = client.get("/operate", follow_redirects=False)
        assert first.status_code in (302, 303, 307)
        assert first.headers["location"] == "/operate/waterfall"
        assert client.get("/operate/aprs").status_code == 200
        again = client.get("/operate", follow_redirects=False)
        assert again.headers["location"] == "/operate/aprs"


def test_the_last_instrument_survives_a_restart(tmp_path):
    with _client(tmp_path) as client:
        client.get("/operate/pager")
    with _client(tmp_path) as client:
        assert client.get("/operate", follow_redirects=False).headers["location"] == \
            "/operate/pager"


def test_an_unknown_instrument_is_a_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/operate/nope", headers={"accept": "text/html"})
    assert resp.status_code == 404


def test_every_instrument_view_renders_with_the_picker_and_back_goes_home(tmp_path):
    with _client(tmp_path) as client:
        for inst in INSTRUMENTS:
            html = client.get(f"/operate/{inst.key}").text
            for other in INSTRUMENTS:
                assert f'href="/operate/{other.key}"' in html
            assert re.search(r'class="btn-back" href="/"', html)
            # The picked instrument reads as current, and only it.
            assert html.count('aria-current="page"') == 1
            assert re.search(rf'href="/operate/{inst.key}"[^>]*aria-current="page"', html)
            # One command strip under the panel, filled from the API's `command` field. The rack
            # has the room to show its whole line, so it is a display, never a tap target.
            assert html.count("data-command ") + html.count("data-command\n") >= 1
            assert re.search(r'<div class="cmdstrip cmdstrip--rack"[^>]*data-command', html)
            assert "data-command-scope" in html
            assert "data-rack-log" in html


@pytest.mark.parametrize("key, hook", [
    ("waterfall", "canvas class=\"waterfall\""), ("ism", "data-decode-panel"),
    ("adsb", "data-adsb-panel"), ("aprs", "data-aprs-panel"), ("pager", "data-pocsag-panel"),
    ("acars", "data-acars-panel"), ("ais", "data-ais-panel"), ("rds", "data-rds-panel"),
    ("dmr", "data-dmr-panel"), ("apt", "data-apt-panel"), ("sstv", "data-sstv-panel"),
    ("lora", "data-lora-panel"), ("flock", "data-flock-panel"),
    ("rayhunter", "data-rayhunter-panel"), ("clock", "data-ppm-panel"),
    ("health", "data-prov-panel"),
])
def test_each_view_draws_the_same_panel_the_lessons_do(tmp_path, key, hook):
    with _client(tmp_path) as client:
        assert hook in client.get(f"/operate/{key}").text


def test_the_waterfall_view_has_free_tuning_a_step_key_and_gain(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/operate/waterfall").text
    assert "data-tune-strip" in html
    assert 'data-tune-step="-1"' in html and 'data-tune-step="1"' in html
    assert "data-step-cycle" in html
    assert 'data-gain-step="-1"' in html and "data-gain-value" in html


def test_a_tunable_decoder_view_carries_its_band_and_current_frequency(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/operate/aprs").text
    assert 'data-decoder-tune="aprs"' in html
    assert 'data-freq-hz="144390000"' in html
    assert 'data-band-low="144000000"' in html and 'data-band-high="148000000"' in html
    assert "144.390" in html


def test_a_fixed_instrument_has_no_tune_keys(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/operate/adsb").text
    assert "data-decoder-tune" not in html
    assert "data-tune-step" not in html
    assert "1090" in html


def test_the_pager_view_says_its_text_is_withheld_on_the_paging_band(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/operate/pager").text
    assert "929.6125" in html
    assert "text withheld" in html.lower()


def test_home_has_the_operator_mode_bar(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/").text
    assert 'href="/operate"' in html
    assert "Operator mode" in html
    assert "The radios, no script" in html


# --- the sweep: tuned to a target, and its refusals logged once per streak --------------------


def test_tuning_to_a_target_offsets_the_sweep_and_the_dial_reads_the_target(tmp_path):
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper) as client:
        resp = client.post("/api/tune", json={"center": 433_920_000, "span": 2_000_000,
                                               "bin_size": 15_625})
        assert resp.status_code == 200
        body = resp.json()
        low, high, bin_size = target_span(433_920_000, 2_000_000, 15_625)
        assert body == {"tuned": True, "low": low, "high": high, "bin_size": bin_size,
                        "center": 433_920_000}
        spectrum = client.get("/api/spectrum").json()
    assert spectrum["center"] == 433_920_000
    assert (spectrum["freq_low"], spectrum["freq_high"]) == (low, high)


def test_a_span_given_by_its_edges_reports_its_middle_as_the_centre(tmp_path):
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper) as client:
        spectrum = client.get("/api/spectrum").json()
    assert spectrum["center"] == (DEFAULT_LOW + DEFAULT_HIGH) // 2


def test_a_target_the_radio_cannot_reach_is_refused_with_the_reason(tmp_path):
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper) as client:
        resp = client.post("/api/tune", json={"center": 5_000_000, "span": 2_000_000})
    assert resp.status_code == 400
    assert resp.json()["tuned"] is False
    assert "MHz" in resp.json()["reason"]


def test_a_refused_sweep_is_logged_once_per_streak_not_per_poll(tmp_path):
    def no_radio():
        return Sweeper(spawn=lambda cmd: Child(cmd), radio_present=lambda: False)

    with _client(tmp_path, sweeper_factory=no_radio) as client:
        for _ in range(4):
            assert client.get("/api/spectrum").status_code == 503
    assert len(_events(tmp_path, "spectrum_blocked")) == 1


# --- decoder frequency overrides ---------------------------------------------------------------


def _aprs_factory(spawned, lines=()):
    def spawn(freq_hz, device):
        child = Child(["rtl_fm", "-M", "fm", "-f", str(freq_hz), "-g", "49.6", "-d",
                       str(device)], said="\n".join(lines))
        spawned.append(freq_hz)
        return child

    return lambda: AprsDecoder(spawn=spawn, **_tools())


def test_a_decoder_retunes_inside_its_band_and_the_next_poll_runs_there(tmp_path):
    spawned = []
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned)) as client:
        client.get("/api/aprs")
        resp = client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})
        assert resp.status_code == 200
        assert resp.json() == {"tuned": True, "instrument": "aprs", "freq_hz": 144_800_000}
        body = client.get("/api/aprs").json()
    assert body["freq_hz"] == 144_800_000
    assert spawned == [144_390_000, 144_800_000]
    assert _events(tmp_path, "decoder_tune")[-1]["payload"] == {
        "instrument": "aprs", "freq_hz": 144_800_000}


def test_a_decoder_refuses_a_frequency_outside_its_band_and_stays_put(tmp_path):
    spawned = []
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned)) as client:
        resp = client.post("/api/aprs/tune", json={"freq_hz": 98_100_000})
        assert resp.status_code == 400
        assert resp.json()["tuned"] is False
        assert "144-148 MHz" in resp.json()["reason"]
        assert client.get("/api/aprs").json()["freq_hz"] == 144_390_000
    assert _events(tmp_path, "decoder_tune_rejected")


def test_a_decoder_tune_with_no_radio_is_a_503_and_changes_nothing(tmp_path):
    spawned = []
    with _client(tmp_path, probe=_absent, aprs_factory=_aprs_factory(spawned)) as client:
        resp = client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})
        assert resp.status_code == 503
        assert "no RTL-SDR" in resp.json()["reason"]
    assert spawned == []


@pytest.mark.parametrize("api", ["adsb", "ais", "spectrum", "nope"])
def test_only_single_channel_decoders_have_a_tune_route(tmp_path, api):
    with _client(tmp_path, probe=_present) as client:
        assert client.post(f"/api/{api}/tune", json={"freq_hz": 1}).status_code == 404


def test_the_pager_text_stays_withheld_when_the_rack_tunes_into_the_paging_band(tmp_path):
    def factory():
        def spawn(freq_hz, device):
            return Child(["rtl_fm", "-f", str(freq_hz)], said=POCSAG_ALPHA)
        return PocsagDecoder(freq_hz=152_007_500, spawn=spawn, **_tools())

    with _client(tmp_path, probe=_present, pocsag_factory=factory) as client:
        assert client.post("/api/pocsag/tune", json={"freq_hz": 930_000_000}).status_code == 200
        client.get("/api/pocsag")
        client.app.state.pocsag.drain()
        pages = client.get("/api/pocsag").json()["pages"]
    assert pages and all(p["text"] is None and p["text_withheld"] for p in pages)
    assert "CALL EXT" not in (tmp_path / "events.jsonl").read_text()


def test_opening_a_lesson_tunes_a_decoder_back_to_its_own_frequency(tmp_path):
    spawned = []
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned)) as client:
        client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})
        client.get("/lesson/1")
        assert client.app.state.aprs.freq_hz == 144_390_000


def _radio_pack(pack_dir, *, tune=""):
    pack_dir.mkdir()
    (pack_dir / "01-radio.yaml").write_text(
        "id: 1\nslug: radio\ntitle: Radio\nconcept: c\ntype: consolidation\n"
        "hardware: [rtl-sdr]\n" + tune +
        "stages:\n  predict: p\n  operate: o\n  observe: s\n  explain: e\n"
        "choices:\n  - key: a\n    label: A\n  - key: b\n    label: B\n", encoding="utf-8")
    return pack_dir


def test_opening_a_lesson_with_no_band_puts_the_sweep_back_on_fm(tmp_path):
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        client.post("/api/tune", json={"center": 433_920_000, "span": 2_000_000})
        client.get("/lesson/1")
        sweeper = client.app.state.sweeper
        assert (sweeper.low, sweeper.high, sweeper.target) == (DEFAULT_LOW, DEFAULT_HIGH, None)


# --- the reception log --------------------------------------------------------------------------


def test_a_live_decode_is_logged_as_a_reception_with_its_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGINT101_RTLSDR_SERIAL", "00000001")
    spawned = []
    factory = _aprs_factory(spawned, lines=[APRS_POS])
    with _client(tmp_path, probe=_present, aprs_factory=factory) as client:
        client.get("/api/aprs")
        client.app.state.aprs.drain()
        recent = client.get("/api/receptions?instrument=aprs").json()
    [event] = _events(tmp_path, "reception")
    payload = event["payload"]
    assert payload["instrument"] == "aprs"
    assert payload["freq_hz"] == 144_390_000
    assert payload["gain"] == "49.6"
    assert payload["ppm"] == {"correction": "raw", "measured_ppm": None}
    assert payload["radio"]["serial"] == "00000001"
    assert payload["command"].startswith("rtl_fm")
    assert payload["item"]["source"] == "KD2ABC-9"
    assert recent["receptions"][0]["payload"]["summary"].startswith("KD2ABC-9")


def test_the_reception_log_lists_newest_first_and_filters_by_instrument(tmp_path):
    def decoder():
        def spawn(cmd):
            return Child(cmd, said=RTL433_ASK)
        return Decoder(spawn=spawn, **_tools())

    spawned = []
    with _client(tmp_path, probe=_present, decoder_factory=decoder,
                 aprs_factory=_aprs_factory(spawned, lines=[APRS_POS])) as client:
        client.get("/api/aprs")
        client.app.state.aprs.drain()
        client.get("/api/decode")
        client.app.state.decoder.drain()
        everything = client.get("/api/receptions").json()["receptions"]
        ism = client.get("/api/receptions?instrument=ism&limit=1").json()["receptions"]
    assert [e["payload"]["instrument"] for e in everything] == ["ism", "aprs"]
    assert [e["payload"]["summary"] for e in ism] == ["Acurite-Tower · 18.0 °C · 51% RH"]


def test_the_reception_log_reads_back_what_an_earlier_run_logged(tmp_path):
    spawned = []
    with _client(tmp_path, probe=_present,
                 aprs_factory=_aprs_factory(spawned, lines=[APRS_POS])) as client:
        client.get("/api/aprs")
        client.app.state.aprs.drain()
    with _client(tmp_path) as client:
        recent = client.get("/api/receptions").json()["receptions"]
    assert [e["payload"]["instrument"] for e in recent] == ["aprs"]


def test_with_nothing_received_the_log_is_honestly_empty(tmp_path):
    with _client(tmp_path) as client:
        body = client.get("/api/receptions").json()
    assert body == {"receptions": [], "error": None}


def test_a_measured_ppm_is_part_of_the_provenance(tmp_path):
    def measure(hz, seconds, device):
        return {"available": True, "hz": hz, "ppm": 3.5, "reason": None}

    spawned = []
    factory = _aprs_factory(spawned, lines=[APRS_POS])
    with _client(tmp_path, probe=_present, aprs_factory=factory,
                 ppm_measure=measure) as client:
        client.post("/api/ppm/measure?hz=98100000")
        client.get("/api/aprs")
        client.app.state.aprs.drain()
    [event] = _events(tmp_path, "reception")
    assert event["payload"]["ppm"] == {"correction": "raw", "measured_ppm": 3.5}
    json.dumps(event)   # the whole record is plain JSON
