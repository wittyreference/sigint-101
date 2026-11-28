# ABOUTME: Integration tests for operator mode's routes: the rack pages, the target-centred sweep
# ABOUTME: tune, the decoder frequency overrides with their band limits, and the reception log.

import json
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101 import childproc, radio_control
from sigint101.app import create_app
from sigint101.aprs import AprsDecoder
from sigint101.decode import Decoder
from sigint101.events import log_event, read_events
from sigint101.flock import FLOCK_SOURCE_ENV, FLOCK_SOURCE_MARAUDER, MarauderFlockDetector
from sigint101.hardware import RadioProbe
from sigint101.operate import INSTRUMENTS
from sigint101.pocsag import PocsagDecoder
from sigint101.spectrum import (
    DEFAULT_HIGH,
    DEFAULT_LOW,
    Sweeper,
    TuneError,
    target_span,
    validate_target_span,
)
from sigint101.units import unit_for_lesson
from tests.child_fakes import Child

FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"

APRS_POS = "APRS: KD2ABC-9>APU25N,WIDE1-1,WIDE2-1:!4042.00N/07400.00W>Test de KD2ABC"
POCSAG_ALPHA = "POCSAG1200: Address: 1234567  Function: 3  Alpha:   CALL EXT 2210 URGENT"
RTL433_ASK = ('{"time" : "2026-10-27 13:04:11", "model" : "Acurite-Tower", "id" : 12345, '
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


def _pick(client, key):
    """The picker's tap: operate.js posts the instrument the learner chose."""
    return client.post("/api/rack/place", json={"instrument": key})


def test_operate_opens_the_waterfall_until_another_instrument_is_used(tmp_path):
    with _client(tmp_path) as client:
        first = client.get("/operate", follow_redirects=False)
        assert first.status_code in (302, 303, 307)
        assert first.headers["location"] == "/operate/waterfall"
        assert _pick(client, "aprs").status_code == 200
        assert client.get("/operate/aprs").status_code == 200
        again = client.get("/operate", follow_redirects=False)
        assert again.headers["location"] == "/operate/aprs"


def test_the_last_instrument_survives_a_restart(tmp_path):
    with _client(tmp_path) as client:
        _pick(client, "pager")
    with _client(tmp_path) as client:
        assert client.get("/operate", follow_redirects=False).headers["location"] == \
            "/operate/pager"


def test_showing_an_instrument_page_leaves_the_rack_place_unchanged(tmp_path):
    # A GET has no side effect: a render check, a prefetch or a crawler that loads a rack page
    # must not move where /operate reopens.
    with _client(tmp_path) as client:
        client.get("/operate/ism")
        assert client.app.state.progress.get_place("rack") is None
        assert client.get("/operate", follow_redirects=False).headers["location"] == \
            "/operate/waterfall"
        _pick(client, "pager")
        for key in ("ism", "lora"):
            assert client.get(f"/operate/{key}").status_code == 200
        assert client.app.state.progress.get_place("rack") == "pager"
        assert client.get("/operate", follow_redirects=False).headers["location"] == \
            "/operate/pager"


def test_picking_an_instrument_records_the_rack_place(tmp_path):
    with _client(tmp_path) as client:
        resp = _pick(client, "ism")
        assert resp.status_code == 200
        assert resp.json() == {"place": "ism"}
        assert client.app.state.progress.get_place("rack") == "ism"


@pytest.mark.parametrize("body", [{"instrument": "nope"}, {"instrument": ""}, {},
                                  {"instrument": ["ism"]}, {"instrument": 7}])
def test_picking_an_instrument_the_rack_lacks_changes_nothing(tmp_path, body):
    with _client(tmp_path) as client:
        _pick(client, "aprs")
        resp = client.post("/api/rack/place", json=body)
        assert resp.status_code == 404
        assert client.app.state.progress.get_place("rack") == "aprs"


def test_every_picker_entry_names_the_instrument_it_records(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/operate/waterfall").text
    picks = re.findall(r'<a class="rack__pick[^"]*" href="/operate/([^"]+)"[^>]*'
                       r'data-rack-pick="([^"]+)"', html)
    assert sorted(href for href, _ in picks) == sorted(inst.key for inst in INSTRUMENTS)
    assert all(href == key for href, key in picks)


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


def test_rack_picker_groups_every_instrument_with_a_short_description(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/operate/ais").text
    picker = re.search(r'<nav class="rack__picker"[^>]*>(.*?)</nav>', html, re.S)[1]
    groups = re.findall(r'<section class="rack__group"[^>]*>\s*<h2[^>]*>([^<]+)</h2>'
                        r'(.*?)</section>', picker, re.S)
    expected = {
        "Spectrum": {"waterfall", "rds", "apt"},
        "Aircraft and ships": {"adsb", "acars", "ais"},
        "Pagers and packets": {"ism", "aprs", "pager", "dmr", "sstv", "lora"},
        "Counter-surveillance": {"flock", "rayhunter"},
        "Devices": {"flipper", "clock", "health"},
    }
    assert [name for name, _ in groups] == list(expected)
    keys = []
    for name, group in groups:
        links = re.findall(r'<a class="rack__pick[^"]*" href="/operate/([^"]+)"[^>]*>'
                           r'(.*?)</a>', group, re.S)
        assert {key for key, _ in links} == expected[name]
        for key, link in links:
            keys.append(key)
            detail = re.search(r'class="rack__pick-detail">([^<]+)</span>', link)
            assert detail is not None, key
            assert 2 <= len(unescape(detail[1]).split()) <= 3, key
    assert sorted(keys) == sorted(inst.key for inst in INSTRUMENTS)


@pytest.mark.parametrize("key, hook", [
    ("waterfall", "canvas class=\"waterfall\""), ("ism", "data-decode-panel"),
    ("adsb", "data-adsb-panel"), ("aprs", "data-aprs-panel"), ("pager", "data-pocsag-panel"),
    ("acars", "data-acars-panel"), ("ais", "data-ais-panel"), ("rds", "data-rds-panel"),
    ("dmr", "data-dmr-panel"), ("apt", "data-apt-panel"), ("sstv", "data-sstv-panel"),
    ("lora", "data-lora-panel"), ("flock", "data-flock-panel"),
    ("rayhunter", "data-rayhunter-panel"), ("flipper", "data-flipper-panel"),
    ("clock", "data-ppm-panel"),
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


def test_the_flipper_view_reads_the_device_over_usb_with_nothing_to_tune(tmp_path):
    # The Flipper is its own USB device: no dial to turn, no SDR to claim, and no receptions to
    # log yet; its panel polls /api/flipper like the lesson's and says plainly when none is there.
    with _client(tmp_path) as client:
        html = client.get("/operate/flipper").text
    assert 'src="/static/flipper.js"' in html
    assert "data-decoder-tune" not in html and "data-tune-step" not in html
    assert "USB" in html


def test_the_pager_view_says_its_text_is_withheld_on_the_paging_band(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/operate/pager").text
    assert "929.6125" in html
    assert "text withheld" in html.lower()


@pytest.mark.parametrize("freq_hz, dial", [
    (152_840_000, "152.8400"),               # a carrier paging channel on VHF (47 CFR 22.531)
    (454_025_000, "454.0250"),               # a Part 22 channel on UHF (47 CFR 22.561)
    (138_000_000, "138.0000"),               # the low edge of the pager's range
])
def test_the_pager_view_says_its_text_is_withheld_off_the_paging_band_too(tmp_path, freq_hz, dial):
    def factory():
        return PocsagDecoder(freq_hz=freq_hz, **_tools())

    with _client(tmp_path, pocsag_factory=factory) as client:
        html = client.get("/operate/pager").text
    assert dial in html
    caption = re.search(r'<div class="tune__caption" data-tune-caption[^>]*>([^<]*)</div>', html)
    assert caption and caption.group(1) == "framing only &middot; text withheld"
    # The dial carries no band inside which the text is withheld: it is withheld everywhere.
    assert "data-text-withheld" in html
    assert "data-withheld-low" not in html and "data-withheld-high" not in html


def test_home_has_the_operator_mode_bar(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/").text
    assert 'href="/operate"' in html
    assert "Operator mode" in html
    assert "The radios, no script" in html


# --- the fixed dials' captions, in the rack and in their lessons -------------------------------

LESSON_PACKS = Path(__file__).resolve().parent.parent / "lesson_packs"

# The caption under a fixed dial. The rack's dial and the lesson's carry the same line.
CAPTION = re.compile(r'<div class="tune__caption"[^>]*>([^<]*)</div>')

# The Flipper's caption names the link and nothing more: "Flipper Zero · serial". Main's "serial,
# receive-only" called the cable receive-only, yet the console types CLI commands down it
# (sigint101/flipper.py), and lesson 29's observe says the cable carries the command line both
# ways. "Serial, radios receive-only" then claimed a state the console does not set: the Flipper's
# Bluetooth advertises whenever its own setting is on (firmware 1.4.3, bt.c), and the console
# sends the Flipper nothing but device_info. So the caption names the serial link and carries no
# posture word: no receive-only label in the words a trim reaches for (lesson 19's set), no
# "read-only" (the console writes CLI commands over that same cable), no "sends nothing", and no
# radio or radio state named at all.
SERIAL = re.compile(r"\bserial\b", re.I)
RECEIVE_ONLY = re.compile(
    r"\breceiv(?:e|es|ing)[\s-]+only\b|\brx[\s-]*only\b|\blisten(?:s|ing)?[\s-]+only\b"
    r"|\b(?:only|just|merely)\s+(?:receiv|listen)\w*|\bpassive(?:ly)?\b|\bone[\s-]way\b", re.I)
READ_ONLY = re.compile(r"\bread[\s-]*only\b|\breads?\s+only\b|\b(?:only|just|merely)\s+reads?\b",
                       re.I)
SENDS_NOTHING = re.compile(
    r"(?:\b(?:never|not|no|nothing|none|without)\b|n't).*?"
    r"\b(?:tx|transmi\w*|send\w*|sent|emit\w*|radiat\w*|broadcast\w*|talk\w*|writ\w*)\b"
    r"|\b(?:transmi\w*|send\w*|emit\w*|writ\w*)\s+(?:nothing|none|no)\b", re.I)
RADIO_STATE = re.compile(
    r"\bradios?\b|\brf\b|\bbluetooth\b|\bble\b|\bsub-?ghz\b|\bnfc\b|\brfid\b|\binfrared\b"
    r"|\bantennas?\b|\bsilen\w*|\bquiet\b|\bidle\b|\boff[\s-]air\b|\bmonitor[\s-]+only\b", re.I)

# The Flock caption named wlan1 ("WiFi · wlan1 monitor mode, receive-only") even where the site
# sets SIGINT101_FLOCK_SOURCE=marauder, whose radio is the Flipper's Wi-Fi devboard read over the
# Flipper's USB-UART bridge (sigint101/flock.py). Either radio listens in monitor mode and sends
# nothing, so a caption may name the radio in use, or none; never the other source's radio or tool.
WLAN1_SOURCE = re.compile(
    r"\bwlan\d*\b|\bwl[a-z]+\d\w*\b|\bmon\d+\b|\btcpdump\b|\badapter\b|\bdongle\b|\brtl8821\w*",
    re.I)
MARAUDER_SOURCE = re.compile(
    r"\bmarauder\b|\bdevboard\b|\besp32\w*|\bflipper\b|\buart\b|\bbridge\b|\bsniffprobe\b", re.I)


def _captions(pages):
    """Each page's fixed-dial captions as the screen reads them (&middot; as a middle dot)."""
    return {where: [unescape(c) for c in CAPTION.findall(page)] for where, page in pages.items()}


def _flipper_postures(caption):
    """The posture words in a caption: claims on what the link, Flipper or radios send."""
    return [m.group(0) for pattern in (RECEIVE_ONLY, READ_ONLY, SENDS_NOTHING, RADIO_STATE)
            for m in pattern.finditer(caption)]


def test_the_flipper_caption_names_the_serial_link_and_no_posture(tmp_path):
    with _client(tmp_path, pack_dir=LESSON_PACKS) as client:
        pages = {"lesson 29": client.get("/lesson/29").text,
                 "the rack": client.get("/operate/flipper").text}
    captions = _captions(pages)
    assert all(captions.values()), f"each page must carry the Flipper dial's caption: {captions}"
    found = [(where, c) for where, cs in captions.items() for c in cs]
    unnamed = [f"{where}: {c}" for where, c in found if not SERIAL.search(c)]
    assert not unnamed, f"a Flipper caption does not name the serial link: {unnamed}"
    claims = [f"{where}: {c} {_flipper_postures(c)}" for where, c in found if _flipper_postures(c)]
    assert not claims, f"a Flipper caption carries a posture word, not only the link: {claims}"


@pytest.mark.parametrize("source, other_source", [
    (None, MARAUDER_SOURCE),                 # unset: tcpdump on wlan1
    (FLOCK_SOURCE_MARAUDER, WLAN1_SOURCE),   # the Flipper's Wi-Fi devboard over its UART bridge
], ids=["wlan1", "marauder"])
def test_the_flock_caption_names_no_radio_but_the_one_the_site_picked(tmp_path, monkeypatch,
                                                                       source, other_source):
    if source is None:
        monkeypatch.delenv(FLOCK_SOURCE_ENV, raising=False)
    else:
        monkeypatch.setenv(FLOCK_SOURCE_ENV, source)
    with _client(tmp_path, pack_dir=LESSON_PACKS) as client:
        # Drawing a page never starts the detector, so neither source touches a radio here.
        on_marauder = isinstance(client.app.state.flock, MarauderFlockDetector)
        pages = {"lesson 18": client.get("/lesson/18").text,
                 "the rack": client.get("/operate/flock").text}
    assert on_marauder == (source == FLOCK_SOURCE_MARAUDER)
    captions = _captions(pages)
    assert all(captions.values()), f"each page must carry the Flock dial's caption: {captions}"
    wrong = [f"{where}: {c}" for where, found in captions.items() for c in found
             if other_source.search(c)]
    assert not wrong, f"a Flock caption names a radio the site did not pick: {wrong}"


# The Flock labels that name the capture itself: the panel's title (lesson 18 and the rack), lesson
# 18's sealed predict frame, and the picker's tool under "Flock" on every rack page. Under the
# Marauder source they said "wlan1" and "tcpdump" while the console typed sniffprobe into Marauder.
# Each names the source the site picked, and never the other one.
FLOCK_TITLE = re.compile(r'data-flock-panel[^>]*>\s*<div class="decoder__head">\s*'
                         r'<span class="decoder__title">([^<]*)</span>')
FLOCK_AXIS = re.compile(r'<div class="decoder__axis"[^>]*>([^<]*)</div>')
FLOCK_PICK = re.compile(r'<a class="rack__pick[^"]*" href="/operate/flock"[^>]*>\s*'
                        r'<span class="rack__pick-label">[^<]*</span>\s*'
                        r'<span class="rack__pick-tool">([^<]*)</span>')


def _use_flock_source(monkeypatch, source):
    if source is None:
        monkeypatch.delenv(FLOCK_SOURCE_ENV, raising=False)
    else:
        monkeypatch.setenv(FLOCK_SOURCE_ENV, source)


@pytest.mark.parametrize("source, picked, other_source", [
    (None, WLAN1_SOURCE, MARAUDER_SOURCE),                   # unset: tcpdump on wlan1
    (FLOCK_SOURCE_MARAUDER, MARAUDER_SOURCE, WLAN1_SOURCE),  # Marauder on the Wi-Fi devboard
], ids=["wlan1", "marauder"])
def test_the_flock_labels_name_the_source_the_site_picked(tmp_path, monkeypatch, source, picked,
                                                          other_source):
    _use_flock_source(monkeypatch, source)
    with _client(tmp_path, pack_dir=LESSON_PACKS) as client:
        # Drawing a page never starts the detector, so neither source touches a radio here.
        lesson = client.get("/lesson/18").text
        rack = client.get("/operate/flock").text
        elsewhere = client.get("/operate/waterfall").text
    labels = {"lesson 18 panel": FLOCK_TITLE.findall(lesson),
              "lesson 18 predict frame": FLOCK_AXIS.findall(lesson),
              "the rack's panel": FLOCK_TITLE.findall(rack),
              "the rack's picker": FLOCK_PICK.findall(rack),
              "the picker on another view": FLOCK_PICK.findall(elsewhere)}
    assert all(labels.values()), f"each page must carry its Flock labels: {labels}"
    found = [(where, unescape(label)) for where, each in labels.items() for label in each]
    wrong = [f"{where}: {label}" for where, label in found if other_source.search(label)]
    assert not wrong, f"a Flock label names a radio or tool the site did not pick: {wrong}"
    unnamed = [f"{where}: {label}" for where, label in found if not picked.search(label)]
    assert not unnamed, f"a Flock label does not name the source the site runs: {unnamed}"


# Lesson 18's tile says what to bring before the lesson opens. "Wi-Fi adapter" named the wlan1
# path's USB adapter, not the Flipper's Wi-Fi devboard that the Marauder source listens with. The
# badge reads the same whichever source the site set, so it names neither source's kit.
LESSON_18_TILE = re.compile(r'href="/lesson/18">(.*?)</a>', re.S)
TILE_NEED = re.compile(r'tile__need">\s*([^<]*?)\s*<')


@pytest.mark.parametrize("source, other_source", [
    (None, MARAUDER_SOURCE),
    (FLOCK_SOURCE_MARAUDER, WLAN1_SOURCE),
], ids=["wlan1", "marauder"])
def test_the_flock_lessons_tile_names_no_kit_the_site_did_not_pick(tmp_path, monkeypatch, source,
                                                                   other_source):
    _use_flock_source(monkeypatch, source)
    with _client(tmp_path, pack_dir=LESSON_PACKS) as client:
        page = client.get(f"/unit/{unit_for_lesson(18).id}").text
    [tile] = LESSON_18_TILE.findall(page)
    needs = [unescape(need) for need in TILE_NEED.findall(tile)]
    assert needs, "lesson 18's tile must say it needs a radio of its own"
    wrong = [need for need in needs if other_source.search(need)]
    assert not wrong, f"lesson 18's tile names kit the site did not pick: {wrong}"


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


def test_a_retune_after_the_tool_died_answers_with_the_new_run_not_the_old_death(tmp_path):
    # C52 (R23): rtl_fm was killed during a decode and the rack retuned at once. The first poll
    # at the new frequency answered with the old run's death and no frequency.
    spawned = []
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned)) as client:
        client.get("/api/aprs")
        decoder = client.app.state.aprs
        decoder._run.proc.kill()                # rtl_fm is killed under the decode
        decoder.drain()                         # and its death is recorded before the retune
        assert client.post("/api/aprs/tune", json={"freq_hz": 144_800_000}).status_code == 200
        resp = client.get("/api/aprs")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["available"] is True and body["state"] == "starting"
    assert body["freq_hz"] == 144_800_000
    assert spawned == [144_390_000, 144_800_000]
    # The old death is dropped with its run, not told as the new run's refusal.
    assert not _events(tmp_path, "aprs_blocked")


def test_a_tool_that_keeps_dying_across_retunes_reads_stopped_at_the_new_frequency(tmp_path):
    # C52 ruling (a): from the second quick death in a row the grown wait survives a retune, and
    # the panel says so in words that name the new frequency rather than the old death's.
    spawned = []
    now = [5000.0]
    decoder = _aprs_factory(spawned)()
    decoder.watch.clock = lambda: now[0]
    with _client(tmp_path, probe=_present, aprs_factory=lambda: decoder) as client:
        for hz in (144_800_000, 145_010_000):
            client.get("/api/aprs")
            decoder._run.proc.kill()            # rtl_fm is killed again, at once
            decoder.drain()
            assert client.post("/api/aprs/tune", json={"freq_hz": hz}).status_code == 200
        held = client.get("/api/aprs")
        now[0] += 2 * childproc.FIRST_RETRY_S
        back = client.get("/api/aprs")
        decoder.stop()
    assert held.status_code == 503
    body = held.json()
    assert body["state"] == body["cause"] == "stopped"
    assert body["retry_in"] == 2 * childproc.FIRST_RETRY_S
    assert body["reason"] == "rtl_fm keeps dying; it tries again at 145.0100 MHz"
    assert back.status_code == 200 and back.json()["freq_hz"] == 145_010_000
    assert spawned == [144_390_000, 144_800_000, 145_010_000]


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


def test_the_pager_text_stays_withheld_when_the_rack_tunes_out_of_the_paging_band(tmp_path):
    # 152.840 MHz is a carrier paging channel on VHF: the framing is served and logged there, and
    # the words reach neither the API, the reception log nor the event file.
    def factory():
        def spawn(freq_hz, device):
            return Child(["rtl_fm", "-f", str(freq_hz)], said=POCSAG_ALPHA)
        return PocsagDecoder(spawn=spawn, **_tools())

    with _client(tmp_path, probe=_present, pocsag_factory=factory) as client:
        assert client.app.state.pocsag.freq_hz == 929_612_500
        assert client.post("/api/pocsag/tune", json={"freq_hz": 152_840_000}).status_code == 200
        client.get("/api/pocsag")
        client.app.state.pocsag.drain()
        resp = client.get("/api/pocsag")
        log = client.get("/api/receptions?instrument=pager")
    body = resp.json()
    assert body["freq_hz"] == 152_840_000
    [page] = body["pages"]
    assert page["text"] is None and page["text_withheld"] is True
    assert (page["address"], page["function"], page["baud"], page["kind"], page["length"]) == (
        1234567, 3, 1200, "alpha", 20)
    [reception] = log.json()["receptions"]
    assert reception["payload"]["freq_hz"] == 152_840_000
    assert "text withheld" in reception["payload"]["summary"]
    for served in (resp.text, log.text, (tmp_path / "events.jsonl").read_text()):
        assert "CALL EXT" not in served


def _open_lesson(client, lesson_id):
    """What opening a lesson does: the page's GET, then its tuning post (lesson-tuning.js)."""
    assert client.get(f"/lesson/{lesson_id}").status_code == 200
    assert client.post(f"/api/tuning?lesson_id={lesson_id}").status_code == 200


def test_opening_a_lesson_tunes_a_decoder_back_to_its_own_frequency(tmp_path):
    spawned = []
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned)) as client:
        client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})
        _open_lesson(client, 1)
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
        _open_lesson(client, 1)
        sweeper = client.app.state.sweeper
        assert (sweeper.low, sweeper.high, sweeper.target) == (DEFAULT_LOW, DEFAULT_HIGH, None)


# --- a slow lesson restore against later tunes and polls (C81) ---------------------------------
# The lesson page posts /api/tuning without blocking, so a tune or a panel poll can arrive while a
# slow restore is still running. Each test holds the restore inside the ISM decoder's retune (the
# first receiver it resets), as a tool that is slow to stop does, and releases it on cue.

JOIN_S = 10.0   # no request may take this long: past it the test reports a hang


class _HeldRetune:
    """Holds one receiver's retune until released."""

    def __init__(self, receiver):
        self.entered = threading.Event()
        self.release = threading.Event()
        real = receiver.retune

        def retune(*args, **kwargs):
            self.entered.set()
            assert self.release.wait(JOIN_S), "the held retune was never released"
            return real(*args, **kwargs)

        receiver.retune = retune


class _Request(threading.Thread):
    """One request on its own thread, so the test can act while it is in flight."""

    def __init__(self, send):
        super().__init__(daemon=True)
        self._send = send
        self.response = None
        self.took = None

    def run(self):
        started = time.monotonic()
        self.response = self._send()
        self.took = time.monotonic() - started

    def finish(self):
        self.join(JOIN_S)
        assert not self.is_alive(), "the request hung"
        return self.response


@contextmanager
def _slow_restore(client):
    """Lesson 1's restore, held in the ISM decoder's retune until the test releases it.

    It is released on any way out of the block, so a failed assertion never leaves a request held
    inside the console while the client shuts it down.
    """
    client.post("/api/decode/tune", json={"freq_hz": 434_000_000})   # the rack left it here
    held = _HeldRetune(client.app.state.decoder)
    restore = _Request(lambda: client.post("/api/tuning?lesson_id=1"))
    restore.start()
    try:
        assert held.entered.wait(JOIN_S)
        yield held, restore
    finally:
        held.release.set()
        restore.join(JOIN_S)


def test_a_late_restore_never_undoes_the_next_lessons_band_tune(tmp_path):
    # The review's race: lesson A's restore is still running when lesson B's panels time out and
    # tune the sweep to B's band. A's restore must not then put the sweep back on FM.
    packs = _radio_pack(tmp_path / "packs")            # lesson 1 has no band: it resets the sweep
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        with _slow_restore(client) as (held, restore):
            band = _Request(lambda: client.post(
                "/api/tune", json={"center": 433_920_000, "span": 2_000_000}))
            band.start()
            time.sleep(0.2)
            held.release.set()
            assert restore.finish().status_code == 200
            assert band.finish().status_code == 200
        sweeper = client.app.state.sweeper
        assert sweeper.target == 433_920_000
        assert (sweeper.low, sweeper.high) != (DEFAULT_LOW, DEFAULT_HIGH)


@pytest.mark.parametrize("route, body, where", [
    ("/api/tune", {"low": 100_000_000, "high": 102_000_000},
     lambda state: (state.sweeper.low, state.sweeper.high) == (100_000_000, 102_000_000)),
    ("/api/aprs/tune", {"freq_hz": 144_800_000},
     lambda state: state.aprs.freq_hz == 144_800_000),
], ids=["sweep", "aprs"])
def test_a_learners_retune_survives_a_late_restore(tmp_path, route, body, where):
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        with _slow_restore(client) as (held, restore):
            tune = _Request(lambda: client.post(route, json=body))
            tune.start()
            time.sleep(0.2)
            held.release.set()
            assert restore.finish().status_code == 200
            assert tune.finish().status_code == 200
        assert where(client.app.state), "the late restore undid the learner's tune"


def test_a_poll_during_a_restore_waits_and_starts_on_the_lessons_frequency(tmp_path):
    spawned = []
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned),
                 sweeper_factory=_sweeper, pack_dir=packs) as client:
        client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})   # the rack left it here
        with _slow_restore(client) as (held, restore):
            poll = _Request(lambda: client.get("/api/aprs"))
            poll.start()
            time.sleep(0.3)
            assert spawned == [], "a poll started its receiver while the restore was still running"
            held.release.set()
            assert restore.finish().status_code == 200
            assert poll.finish().status_code == 200
        assert spawned == [144_390_000]


def test_a_poll_or_tune_during_a_stuck_restore_is_refused_within_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(radio_control, "TUNING_WAIT_S", 0.3, raising=False)
    spawned = []
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned),
                 sweeper_factory=_sweeper, pack_dir=packs) as client:
        client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})
        before = (client.app.state.sweeper.low, client.app.state.sweeper.high)
        with _slow_restore(client) as (held, restore):
            poll = _Request(lambda: client.get("/api/aprs"))
            poll.start()
            answer = poll.finish()
            assert answer.status_code == 503
            assert answer.json()["reason"] == "tuning the radios for this lesson"
            assert poll.took < 3.0
            assert spawned == []

            tune = _Request(lambda: client.post(
                "/api/tune", json={"center": 433_920_000, "span": 2_000_000}))
            tune.start()
            refused = tune.finish()
            assert refused.status_code == 409
            assert refused.json() == {"tuned": False,
                                      "reason": "tuning the radios for this lesson"}
            assert tune.took < 3.0
            assert (client.app.state.sweeper.low, client.app.state.sweeper.high) == before
            held.release.set()
            assert restore.finish().status_code == 200
        assert client.get("/api/aprs").status_code == 200
        assert spawned == [144_390_000]


BAND_HZ, BAND_SPAN_HZ = 433_920_000, 2_000_000


def _two_lesson_packs(pack_dir):
    """Lesson 1 has no band (its restore puts the sweep on FM); lesson 2 has the 433.92 MHz band."""
    _radio_pack(pack_dir)
    (pack_dir / "02-band.yaml").write_text(
        "id: 2\nslug: band\ntitle: Band\nconcept: c\ntype: consolidation\n"
        "hardware: [rtl-sdr]\n"
        f"tune:\n  center_hz: {BAND_HZ}\n  span_hz: {BAND_SPAN_HZ}\n"
        "stages:\n  predict: p\n  operate: o\n  observe: s\n  explain: e\n"
        "choices:\n  - key: a\n    label: A\n  - key: b\n    label: B\n", encoding="utf-8")
    return pack_dir


def _on_the_band(sweeper):
    low, high, bin_size = validate_target_span(BAND_HZ, BAND_SPAN_HZ)
    return (sweeper.low, sweeper.high, sweeper.bin_size, sweeper.target) == (
        low, high, bin_size, BAND_HZ)


def test_opening_a_lesson_with_a_band_puts_the_sweep_on_it(tmp_path):
    # The restore sets the band itself, as the waterfall's band tune would, so a band tune of the
    # page's own can never be refused and leave the lesson off its band.
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        _open_lesson(client, 2)
        assert _on_the_band(client.app.state.sweeper)


def test_the_next_lessons_band_lands_after_a_stuck_restore_from_the_lesson_before(tmp_path):
    # The review's case: lesson A's restore is stuck in a step when the learner moves on to
    # lesson B, which has a band. B's restore waits its turn, then sets B's band; until it does,
    # it has not answered, so B's rail says "Tuning…" rather than nothing.
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        sweeper = client.app.state.sweeper
        with _slow_restore(client) as (held, restore_a):
            restore_b = _Request(lambda: client.post("/api/tuning?lesson_id=2"))
            restore_b.start()
            time.sleep(0.3)
            assert not _on_the_band(sweeper)
            assert restore_b.is_alive(), "B's restore answered with the sweep still off its band"
            held.release.set()
            assert restore_a.finish().status_code == 200
            assert restore_b.finish().status_code == 200
        assert _on_the_band(sweeper), "B's sweep ended off the band its lesson names"


def test_a_restore_that_raises_lets_go_of_the_radios(tmp_path):
    # A restore that fails part-way must not leave every later claim and tune refused.
    spawned = []
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned),
                 sweeper_factory=_sweeper, pack_dir=packs) as client:
        client.post("/api/decode/tune", json={"freq_hz": 434_000_000})

        def broken(*args, **kwargs):
            raise RuntimeError("rtl_433 would not stop")

        client.app.state.decoder.retune = broken
        with pytest.raises(RuntimeError):
            client.post("/api/tuning?lesson_id=1")
        started = time.monotonic()
        assert client.get("/api/aprs").status_code == 200
        assert client.post("/api/tune", json={"low": 100_000_000,
                                              "high": 102_000_000}).status_code == 200
        assert time.monotonic() - started < 1.0, "a claim or a tune waited on a dead restore"


# --- round 4 (Codex D-123): claims queued at the arbiter, band refusals, skipped steps, order ---


def _epoch(client):
    """The console's clock epoch, as the page reads it from its clock meta (or none)."""
    return getattr(client.app.state.tuning_gate, "epoch", "")


def _stamped(client, at):
    """A learner action's stamp as the page sends it: the clock epoch and the action's time."""
    return f"&epoch={_epoch(client)}&at={at}"


class _PausedRegistration:
    """Holds the next restore just before it registers with the gate, as a slow request is held
    in transit or in a busy worker after the learner has already moved on."""

    def __init__(self, gate):
        self.reached = threading.Event()
        self.release = threading.Event()
        real = gate.begin_restore

        def begin(*args, **kwargs):
            gate.begin_restore = real
            self.reached.set()
            assert self.release.wait(JOIN_S), "the paused restore was never released"
            return real(*args, **kwargs)

        gate.begin_restore = begin


def test_a_claim_queued_at_the_arbiter_never_starts_inside_a_restore(tmp_path):
    # Codex F1: an APRS poll passes the gate while it is idle and queues at the arbiter behind
    # another claim. A lesson's restore then begins and pauses before it reaches APRS. When the
    # arbiter frees, the poll must not start APRS on the rack's 144.800 MHz: it lets the radio
    # go, waits out the restore within its budget, and starts on the lesson's 144.390 MHz.
    spawned = []
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned),
                 sweeper_factory=_sweeper, pack_dir=packs) as client:
        client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})   # the rack left it here
        arbiter = client.app.state.radio_arbiter
        holding, let_go = threading.Event(), threading.Event()

        def hold_the_radio():
            with arbiter.reserve("ppm", RuntimeError, time.monotonic() + JOIN_S):
                holding.set()
                let_go.wait(JOIN_S)

        holder = threading.Thread(target=hold_the_radio, daemon=True)
        holder.start()
        assert holding.wait(JOIN_S)
        try:
            poll = _Request(lambda: client.get("/api/aprs"))
            poll.start()
            time.sleep(0.2)                  # past the idle gate, queued at the arbiter
            with _slow_restore(client) as (held, restore):
                let_go.set()                 # the queued claim gets the radio mid-restore
                time.sleep(0.3)
                assert spawned == [], "a queued claim started its tool inside a restore"
                held.release.set()
                assert restore.finish().status_code == 200
            assert poll.finish().status_code == 200
        finally:
            let_go.set()
            holder.join(JOIN_S)
        assert spawned == [144_390_000]


def test_a_restore_waits_for_a_launch_already_under_way(tmp_path):
    # The permit's other half: a claim admitted before the restore began finishes its launch
    # before the restore takes a step, so the restore then stops that tool and the next poll
    # starts it on the lesson's frequency, rather than both racing on the receiver at once.
    launching, finish = threading.Event(), threading.Event()
    spawned = []

    def slow_spawn(freq_hz, device):
        launching.set()
        assert finish.wait(JOIN_S), "the held launch was never released"
        spawned.append(freq_hz)
        return Child(["rtl_fm", "-f", str(freq_hz), "-d", str(device)])

    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs,
                 aprs_factory=lambda: AprsDecoder(spawn=slow_spawn, **_tools())) as client:
        client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})
        client.post("/api/decode/tune", json={"freq_hz": 434_000_000})
        steps = []
        real = client.app.state.decoder.retune
        client.app.state.decoder.retune = lambda hz: (steps.append(list(spawned)), real(hz))
        poll = _Request(lambda: client.get("/api/aprs"))
        poll.start()
        try:
            assert launching.wait(JOIN_S)
            restore = _Request(lambda: client.post("/api/tuning?lesson_id=1"))
            restore.start()
            time.sleep(0.3)
            assert steps == [], "the restore took a step while a launch was under way"
        finally:
            finish.set()
        assert poll.finish().status_code == 200
        assert restore.finish().json() == {"tuned": True, "lesson_id": 1}
        assert steps == [[144_800_000]]                    # its first step came after the launch
        assert client.app.state.aprs.freq_hz == 144_390_000


def _narrow_band_packs(pack_dir):
    """Lesson 3's band is 100 kHz wide: a pack the loader takes, but too narrow a sweep for the
    default 125 kHz bin."""
    pack_dir.mkdir()
    (pack_dir / "03-narrow.yaml").write_text(
        "id: 3\nslug: narrow\ntitle: Narrow\nconcept: c\ntype: consolidation\n"
        "hardware: [rtl-sdr]\n"
        f"tune:\n  center_hz: {BAND_HZ}\n  span_hz: 100000\n"
        "stages:\n  predict: p\n  operate: o\n  observe: s\n  explain: e\n"
        "choices:\n  - key: a\n    label: A\n  - key: b\n    label: B\n", encoding="utf-8")
    return pack_dir


def test_a_lesson_band_the_sweep_cannot_take_is_refused_with_the_validators_reason(tmp_path):
    # Codex F2: the band's TuneError was a bare 500, so the rail could only say "error 500".
    with pytest.raises(TuneError) as validator:
        validate_target_span(BAND_HZ, 100_000)
    packs = _narrow_band_packs(tmp_path / "packs")
    app = create_app(pack_dir=packs, data_dir=tmp_path, probe=_present,
                     sweeper_factory=_sweeper)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/api/tuning?lesson_id=3")
    assert resp.status_code == 422
    body = resp.json()
    assert body["tuned"] is False
    assert body["reason"] == str(validator.value)


def test_a_restore_step_skipped_for_the_learners_tune_answers_so(tmp_path):
    # Codex F3: a step the restore skipped was dropped and the answer was "tuned: true", so the
    # rail cleared as if the radios were on the lesson's frequencies.
    # Codex D-139 F5: which of the restore's next step and the waiting tune takes the step lock
    # first is up to the scheduler, so the restore is held at its sweep step until the learner's
    # tune has landed (a barrier, not a sleep).
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        gate = client.app.state.tuning_gate
        tuned = threading.Event()
        real_step = gate.restore_step

        def step(receiver, *args):
            if receiver == "sweeper":
                assert tuned.wait(JOIN_S), "the learner's tune never landed"
            return real_step(receiver, *args)

        gate.restore_step = step
        with _slow_restore(client) as (held, restore):
            def learner_tune():
                try:
                    return client.post("/api/tune", json={"low": 100_000_000,
                                                          "high": 102_000_000})
                finally:
                    tuned.set()

            tune = _Request(learner_tune)
            tune.start()
            held.release.set()
            answer = restore.finish()
            assert tune.finish().status_code == 200
        sweeper = client.app.state.sweeper
        assert (sweeper.low, sweeper.high) == (100_000_000, 102_000_000)
    assert answer.status_code == 200
    assert answer.json()["tuned"] is False
    assert answer.json()["skipped"] == {"sweeper": "you retuned"}


def test_a_restore_overtaken_by_the_next_lesson_answers_so(tmp_path):
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        with _slow_restore(client) as (held, restore_a):
            restore_b = _Request(lambda: client.post("/api/tuning?lesson_id=2"))
            restore_b.start()
            time.sleep(0.3)
            held.release.set()
            answer = restore_a.finish()
            assert restore_b.finish().json() == {"tuned": True, "lesson_id": 2}
    assert answer.status_code == 200
    assert answer.json()["tuned"] is False
    assert set(answer.json()["skipped"].values()) == {"a newer lesson"}


def test_an_older_lessons_restore_that_registers_late_never_undoes_the_next_lesson(tmp_path):
    # Codex F4: lesson 1's request was sent first but paused before it registered; lesson 2's
    # restore set its band meanwhile. The order of the learner's actions, which the page stamps,
    # decides, not the order the requests reach the gate: lesson 1's is refused before it starts.
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        paused = _PausedRegistration(client.app.state.tuning_gate)
        older = _Request(lambda: client.post("/api/tuning?lesson_id=1" + _stamped(client, 1000)))
        older.start()
        assert paused.reached.wait(JOIN_S)
        try:
            newer = client.post("/api/tuning?lesson_id=2" + _stamped(client, 2000))
            assert newer.json() == {"tuned": True, "lesson_id": 2}
        finally:
            paused.release.set()
        late = older.finish()
        assert _on_the_band(client.app.state.sweeper), "a stale restore put the sweep back on FM"
    assert late.status_code == 409
    assert late.json()["stale"] is True


def test_an_older_lessons_restore_that_registers_late_never_undoes_a_manual_tune(tmp_path):
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        paused = _PausedRegistration(client.app.state.tuning_gate)
        older = _Request(lambda: client.post("/api/tuning?lesson_id=1" + _stamped(client, 1000)))
        older.start()
        assert paused.reached.wait(JOIN_S)
        try:
            tuned = client.post("/api/tune", json={"low": 100_000_000, "high": 102_000_000,
                                                   "epoch": _epoch(client), "at": 2000})
            assert tuned.status_code == 200
        finally:
            paused.release.set()
        late = older.finish()
        sweeper = client.app.state.sweeper
        assert (sweeper.low, sweeper.high) == (100_000_000, 102_000_000)
    assert late.status_code == 200
    assert late.json()["skipped"] == {"sweeper": "you retuned"}


@pytest.mark.parametrize("newer", ["lesson", "tune"])
def test_a_restore_that_reaches_the_console_late_is_ordered_by_the_learners_action(
        tmp_path, newer):
    # The delay can come before the handler too (the network, a busy worker pool), so arrival
    # order is not enough: the page's stamp says lesson 1 was opened before the newer action.
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        if newer == "lesson":
            first = client.post("/api/tuning?lesson_id=2" + _stamped(client, 2000))
        else:
            first = client.post("/api/tune", json={"low": 100_000_000, "high": 102_000_000,
                                                   "epoch": _epoch(client), "at": 2000})
        assert first.status_code == 200
        late = client.post("/api/tuning?lesson_id=1" + _stamped(client, 1000))
        sweeper = client.app.state.sweeper
        if newer == "lesson":
            assert late.status_code == 409 and late.json()["stale"] is True
            assert _on_the_band(sweeper)
        else:
            assert late.json()["skipped"] == {"sweeper": "you retuned"}
            assert (sweeper.low, sweeper.high) == (100_000_000, 102_000_000)


def test_a_tune_sent_before_the_newest_lesson_opened_is_refused(tmp_path):
    # A +/- step from the lesson before, still in flight when the next lesson's restore ran,
    # must not move the next lesson's sweep.
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        assert client.post("/api/tuning?lesson_id=2" + _stamped(client, 2000)).status_code == 200
        late = client.post("/api/tune", json={"low": 100_000_000, "high": 102_000_000,
                                              "epoch": _epoch(client), "at": 1000})
        assert late.status_code == 409
        assert late.json()["tuned"] is False
        assert _on_the_band(client.app.state.sweeper)


def test_every_page_carries_the_consoles_clock_for_its_action_stamps(tmp_path):
    # The page stamps each learner action on the console's own clock: the epoch and the
    # console's time when it rendered the page. Rendering only reads the clock.
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        gate = client.app.state.tuning_gate
        for url in ("/lesson/2", "/operate/waterfall"):
            html = client.get(url).text
            meta = re.search(r'<meta name="sigint101-clock" content="([0-9a-f]+) ([0-9.]+)">',
                             html)
            assert meta, f"{url} carries no clock for its action stamps"
            assert meta.group(1) == _epoch(client)
            assert 0 < float(meta.group(2)) <= gate.now_ms()


# --- round 5 (Codex D-132): one timeline, arrival before the probe, ppm's launch, the deadline ---

ACTION_CLOCK = Path(__file__).resolve().parent.parent / "sigint101" / "static" / "action-clock.js"


def _browser_stamps(anchors, script):
    """Stamps the real action-clock.js makes, run under node.

    `anchors` are the clock metas of the tabs (epoch, ms). `script` is a list of
    [tab, received, script_at, act_at] on the browser's shared timeline: when the tab's response
    arrived, when its script ran, and when the learner acted in it. Returns each action's stamp.
    """
    node = shutil.which("node")
    assert node, "node is needed to run action-clock.js"
    program = (
        "const c = require(process.argv[1]);"
        "const [anchors, script] = JSON.parse(process.argv[2]);"
        "let now = 0;"
        "const clocks = anchors.map(([epoch, ms], i) => {"
        "  const [, received, scriptAt] = script.find(s => s[0] === i);"
        "  now = scriptAt; return c.makeClock({epoch, ms}, () => now, received); });"
        "console.log(JSON.stringify(script.map(([tab, , , actAt]) => {"
        "  now = actAt; return clocks[tab].stamp(); })));"
    )
    out = subprocess.run([node, "-e", program, str(ACTION_CLOCK), json.dumps([anchors, script])],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def _clock_meta(client, url):
    html = client.get(url).text
    meta = re.search(r'<meta name="sigint101-clock" content="(\S+) ([0-9.]+)">', html)
    assert meta, f"{url} carries no clock meta"
    return meta.group(1), float(meta.group(2))


def test_a_later_action_in_a_slow_tab_wins_over_an_earlier_one_in_a_fast_tab(tmp_path):
    # Codex D-132 F1, through the endpoints with stamps the page's own clock makes. Tab A opened
    # lesson 1 (no band) and its script ran at once; tab B opened lesson 2 (a band) a moment
    # later, but its script ran 300 ms late. The learner acted in A first, then in B. A's restore
    # reaches the console last and must not put B's band back to FM.
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        epoch, render_a = _clock_meta(client, "/lesson/1")
        _, render_b = _clock_meta(client, "/lesson/2")
        browser = 10_000.0                       # the browser timeline when A's response arrived
        gap = render_b - render_a                # B's response arrived that much later
        script = [[0, browser + 1, browser + 1, browser + 6],
                  [1, browser + gap + 1, browser + gap + 301, browser + gap + 46]]
        stamp_a, stamp_b = _browser_stamps([[epoch, render_a], [epoch, render_b]], script)
        assert stamp_b["at"] > stamp_a["at"]
        newer = client.post("/api/tuning?lesson_id=2"
                            f"&epoch={stamp_b['epoch']}&at={stamp_b['at']}")
        assert newer.json() == {"tuned": True, "lesson_id": 2}
        late = client.post(f"/api/tuning?lesson_id=1&epoch={stamp_a['epoch']}&at={stamp_a['at']}")
        assert late.status_code == 409 and late.json()["stale"] is True
        assert _on_the_band(client.app.state.sweeper)


def test_an_unstamped_tune_is_ordered_by_its_arrival_not_by_when_its_probe_ended(tmp_path):
    # Codex D-132 F3: the first tune arrived first but was held in its hardware probe; the second
    # arrived after it and landed. When the first comes out of its probe it is the older of the
    # two, by arrival, and must not overwrite the second.
    entered, release = threading.Event(), threading.Event()
    hold = {"armed": False}

    def probe():
        if hold["armed"]:
            hold["armed"] = False
            entered.set()
            assert release.wait(JOIN_S), "the held probe was never released"
        return _present()

    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=probe, sweeper_factory=_sweeper, pack_dir=packs) as client:
        hold["armed"] = True
        first = _Request(lambda: client.post("/api/tune", json={"low": 100_000_000,
                                                                "high": 102_000_000}))
        first.start()
        assert entered.wait(JOIN_S)
        try:
            second = client.post("/api/tune", json={"low": 110_000_000, "high": 112_000_000})
            assert second.status_code == 200
        finally:
            release.set()
        assert first.finish().status_code == 409
        sweeper = client.app.state.sweeper
        assert (sweeper.low, sweeper.high) == (110_000_000, 112_000_000)


def test_a_calibration_queued_at_the_arbiter_never_launches_inside_a_restore(tmp_path):
    # Codex D-132 F4: a ppm measurement queued behind another claim reached its rtl_fm launch with
    # a restore under way. It now waits out the restore under the same permit as a receiver.
    launched = []

    def measure(**_):
        launched.append(client.app.state.tuning_gate._restoring)
        return {"ppm": 1.0, "reason": None}

    packs = _radio_pack(tmp_path / "packs")
    client = _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs,
                     ppm_measure=measure)
    with client:
        arbiter = client.app.state.radio_arbiter
        holding, let_go = threading.Event(), threading.Event()

        def hold_the_radio():
            with arbiter.reserve("sweeper", RuntimeError, time.monotonic() + JOIN_S):
                holding.set()
                let_go.wait(JOIN_S)

        holder = threading.Thread(target=hold_the_radio, daemon=True)
        holder.start()
        assert holding.wait(JOIN_S)
        try:
            ppm = _Request(lambda: client.post("/api/ppm/measure"))
            ppm.start()
            time.sleep(0.2)                  # queued at the arbiter
            with _slow_restore(client) as (held, restore):
                let_go.set()
                time.sleep(0.3)
                assert launched == [], "the calibration launched inside a restore"
                held.release.set()
                assert restore.finish().status_code == 200
            assert ppm.finish().status_code == 200
        finally:
            let_go.set()
            holder.join(JOIN_S)
        assert launched == [0]


def test_a_queued_claim_is_refused_at_its_deadline_even_if_the_restore_has_just_ended(
        tmp_path, monkeypatch):
    # Codex D-132 F5: a claim sent back by a restore past its tuning deadline started anyway once
    # the restore had cleared. It must be refused: its time to wait on restores is spent.
    monkeypatch.setattr(radio_control, "TUNING_WAIT_S", 0.25)
    spawned = []
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned),
                 sweeper_factory=_sweeper, pack_dir=packs) as client:
        client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})
        gate = client.app.state.tuning_gate
        arbiter = client.app.state.radio_arbiter
        holding, let_go = threading.Event(), threading.Event()

        def hold_the_radio():
            with arbiter.reserve("ppm", RuntimeError, time.monotonic() + JOIN_S):
                holding.set()
                let_go.wait(JOIN_S)

        holder = threading.Thread(target=hold_the_radio, daemon=True)
        holder.start()
        assert holding.wait(JOIN_S)
        try:
            poll = _Request(lambda: client.get("/api/aprs"))
            poll.start()
            time.sleep(0.1)                  # past the idle gate, queued at the arbiter
            with _slow_restore(client) as (held, restore):
                real_admit = gate.admit_claim

                def admit(*args):
                    # Sent back by the restore, which then ends before the claim tries again.
                    if real_admit(*args):
                        return True
                    held.release.set()
                    restore.join(JOIN_S)
                    return False

                gate.admit_claim = admit
                time.sleep(0.3)              # past the claim's 0.25 s deadline, still queued
                let_go.set()
                answer = poll.finish()
        finally:
            let_go.set()
            holder.join(JOIN_S)
        assert answer.status_code == 503
        assert answer.json()["reason"] == "tuning the radios for this lesson"
        assert spawned == []


# --- round 6 (Codex D-139): one conversion per browser, navigation-start stamps, the deadline --

_TABS_PROGRAM = """
const c = require(process.argv[1]);
const {epoch, tabs, acts} = JSON.parse(process.argv[2]);
const stored = {};
const storage = {getItem: k => (k in stored ? stored[k] : null),
                 setItem: (k, v) => { stored[k] = String(v); }};
let now = 0;
const clocks = tabs.map(t => { now = t.script_at;
  return c.makeClock({epoch, ms: t.render_ms}, () => now, t.received, storage, t.origin); });
console.log(JSON.stringify(acts.map(([tab, at]) => {
  if (at === "navigation") return clocks[tab].navigationStamp();
  now = at; return clocks[tab].stamp(); })));
"""


def _tab_stamps(epoch, tabs, acts):
    """Stamps the real action-clock.js makes for several tabs of one browser sharing storage.

    `tabs` give each tab's render time on the console's clock and its navigation start, receive
    time and script start on the browser's timeline; `acts` are [tab, browser time] or
    [tab, "navigation"] for the stamp of the tab's navigation start.
    """
    node = shutil.which("node")
    assert node, "node is needed to run action-clock.js"
    out = subprocess.run([node, "-e", _TABS_PROGRAM, str(ACTION_CLOCK),
                          json.dumps({"epoch": epoch, "tabs": tabs, "acts": acts})],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_a_later_action_in_a_tab_whose_response_was_delayed_wins_at_the_endpoints(tmp_path):
    # Codex D-139 F1: a tab whose response headers were delayed 950 ms after the console rendered
    # it underestimated the console's clock by 950 ms, so its later tune stamped earlier than an
    # earlier tune in a prompt tab, and the earlier tune won. One browser now keeps one
    # conversion, the tightest any of its tabs has seen, so the later action stamps later.
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        epoch = _epoch(client)
        base = client.app.state.tuning_gate.now_ms() - 20_000   # all of it in the past
        skew = 5_000_000.0                       # the browser's timeline runs this far ahead
        prompt = {"render_ms": base, "origin": base + skew - 50,
                  "received": base + skew + 1, "script_at": base + skew + 2}
        delayed = {"render_ms": base + 500, "origin": base + skew + 450,
                   "received": base + skew + 500 + 950, "script_at": base + skew + 1452}
        earlier, later = _tab_stamps(epoch, [prompt, delayed],
                                     [[0, base + skew + 2000], [1, base + skew + 2100]])
        assert later["at"] > earlier["at"]
        tune = {"low": 100_000_000, "high": 102_000_000}
        first = client.post("/api/tune", json={**tune, "epoch": epoch, "at": later["at"]})
        assert first.status_code == 200
        late = client.post("/api/tune", json={"low": 110_000_000, "high": 112_000_000,
                                              "epoch": epoch, "at": earlier["at"]})
        assert late.status_code == 409
        sweeper = client.app.state.sweeper
        assert (sweeper.low, sweeper.high) == (100_000_000, 102_000_000)


def test_a_lessons_first_restore_is_stamped_when_it_was_opened_not_when_its_script_ran(tmp_path):
    # Codex D-139 F2: lesson 1 was opened first but its script ran late; lesson 2, opened after it
    # in another tab, had already set its band. Lesson 1's restore is the learner's act of
    # opening it, so it is stamped at its navigation start and is refused as stale.
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        epoch = _epoch(client)
        base = client.app.state.tuning_gate.now_ms() - 20_000
        skew = 5_000_000.0
        first = {"render_ms": base + 5, "origin": base + skew,
                 "received": base + skew + 6, "script_at": base + skew + 3000}
        second = {"render_ms": base + 1005, "origin": base + skew + 1000,
                  "received": base + skew + 1006, "script_at": base + skew + 1010}
        opened_first, opened_second = _tab_stamps(
            epoch, [first, second], [[0, "navigation"], [1, "navigation"]])
        assert opened_first["at"] < opened_second["at"]
        newer = client.post(f"/api/tuning?lesson_id=2&epoch={epoch}&at={opened_second['at']}")
        assert newer.json() == {"tuned": True, "lesson_id": 2}
        late = client.post(f"/api/tuning?lesson_id=1&epoch={epoch}&at={opened_first['at']}")
        assert late.status_code == 409 and late.json()["stale"] is True
        assert _on_the_band(client.app.state.sweeper)


def test_a_bounced_claim_checks_its_deadline_again_after_its_gate_wait(tmp_path, monkeypatch):
    # Codex D-139 F3: a claim sent back by a restore was paused just before its retry's gate wait
    # while its deadline ran out and the restore finished. The idle gate then let it through, and
    # it launched past its budget. It now checks the deadline once the gate wait is over.
    monkeypatch.setattr(radio_control, "TUNING_WAIT_S", 0.5)
    spawned = []
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned),
                 sweeper_factory=_sweeper, pack_dir=packs) as client:
        client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})
        gate = client.app.state.tuning_gate
        arbiter = client.app.state.radio_arbiter
        holding, let_go = threading.Event(), threading.Event()

        def hold_the_radio():
            with arbiter.reserve("ppm", RuntimeError, time.monotonic() + JOIN_S):
                holding.set()
                let_go.wait(JOIN_S)

        holder = threading.Thread(target=hold_the_radio, daemon=True)
        holder.start()
        assert holding.wait(JOIN_S)
        real_wait = gate.wait_idle
        waits = []
        paused = {}

        def wait_idle(until=None):
            waits.append(until)
            if len(waits) == 2:                      # the bounced claim's retry: held here
                held, restore = paused["restore"]
                held.release.set()                   # while the restore finishes ...
                restore.join(JOIN_S)
                time.sleep(max(0.0, until - time.monotonic()) + 0.05)   # ... and time runs out
            return real_wait(until=until)

        gate.wait_idle = wait_idle
        try:
            poll = _Request(lambda: client.get("/api/aprs"))
            poll.start()
            time.sleep(0.1)                          # past the idle gate, queued at the arbiter
            with _slow_restore(client) as (held, restore):
                paused["restore"] = (held, restore)
                let_go.set()                         # the radio frees mid-restore: bounced
                answer = poll.finish()
        finally:
            let_go.set()
            holder.join(JOIN_S)
        assert len(waits) >= 2
        assert answer.status_code == 503
        assert answer.json()["reason"] == "tuning the radios for this lesson"
        assert spawned == []


# --- after round 5 (the EM's ruling): a calibration is named for what it is -------------------

CALIBRATION_REASON = "measuring the clock error (ppm)"


def test_a_poll_behind_a_calibration_names_the_calibration_not_a_tuning(tmp_path, monkeypatch):
    # A restore that begins during a ppm calibration waits for it (the calibration holds the
    # launch permit for its capture). Meanwhile the radio is held by the calibration, not by any
    # tuning, so a poll that gives up says so, and the gate's read-only state says what the
    # restore waits on. Show only what really happened.
    monkeypatch.setattr(radio_control, "TUNING_WAIT_S", 0.3)
    measuring, finish = threading.Event(), threading.Event()
    spawned = []

    def measure(**_):
        measuring.set()
        assert finish.wait(JOIN_S), "the held calibration was never released"
        return {"ppm": 1.0, "reason": None}

    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned),
                 sweeper_factory=_sweeper, pack_dir=packs, ppm_measure=measure) as client:
        client.post("/api/aprs/tune", json={"freq_hz": 144_800_000})
        assert client.get("/api/tuning").json() == {"restoring": False, "waiting_on": None}
        ppm = _Request(lambda: client.post("/api/ppm/measure"))
        ppm.start()
        try:
            assert measuring.wait(JOIN_S)
            restore = _Request(lambda: client.post("/api/tuning?lesson_id=1"))
            restore.start()
            time.sleep(0.2)
            assert client.get("/api/tuning").json() == {"restoring": True,
                                                        "waiting_on": "calibration"}
            poll = client.get("/api/aprs")
            assert poll.status_code == 503
            assert poll.json()["reason"] == CALIBRATION_REASON
            assert spawned == []
        finally:
            finish.set()
        assert ppm.finish().status_code == 200
        assert restore.finish().json() == {"tuned": True, "lesson_id": 1}
        assert client.get("/api/tuning").json() == {"restoring": False, "waiting_on": None}
        assert client.get("/api/aprs").status_code == 200
        assert spawned == [144_390_000]


def test_every_page_is_served_uncached_so_back_renders_a_fresh_clock(tmp_path):
    # A page the browser served from its HTTP cache on Back still held its old clock meta, so its
    # stamps lagged by as long as the learner had been away, and its restore was refused as stale
    # on the lesson the learner was on. Every page is now fetched again on Back, with a fresh
    # clock, and posts its restore. Saying so changes nothing: the GET stays read-only.
    packs = _two_lesson_packs(tmp_path / "packs")
    with _client(tmp_path, probe=_present, sweeper_factory=_sweeper, pack_dir=packs) as client:
        for url in ("/lesson/1", "/lesson/2", "/operate/waterfall", "/"):
            resp = client.get(url)
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/html")
            assert "no-store" in resp.headers.get("cache-control", ""), url


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


def test_a_stored_pager_reception_is_served_without_its_pages_words(tmp_path):
    # An event log can hold a pager reception that carries a page's words, in its item and in
    # its line. The file is the owner's and stays as it is; nothing read in from it has them.
    log = tmp_path / "events.jsonl"
    log_event(log, "reception", {
        "instrument": "pager", "tool": "multimon-ng", "freq_hz": 152_840_000, "gain": "49.6",
        "ppm": {"correction": "raw", "measured_ppm": None},
        "radio": {"kind": "rtl-sdr", "serial": None, "index": 0},
        "command": "rtl_fm -M fm -f 152840000 -s 22050 -g 49.6 -l 0 -d 0",
        "summary": "cap 1234567 · alpha · 20 chars · CALL EXT 2210 URGENT",
        "item": {"baud": 1200, "address": 1234567, "function": 3, "kind": "alpha",
                 "text": "CALL EXT 2210 URGENT", "length": 20, "text_withheld": False},
    })
    on_disk = log.read_bytes()
    assert b"CALL EXT" in on_disk
    with _client(tmp_path) as client:
        everything = client.get("/api/receptions")
        newest = client.get("/api/receptions?limit=1&instrument=pager")   # the rack's LOG line
    for resp in (everything, newest):
        assert resp.status_code == 200
        assert "CALL EXT" not in resp.text
    [event] = newest.json()["receptions"]
    payload = event["payload"]
    assert payload["item"]["text"] is None
    assert payload["item"]["text_withheld"] is True
    assert payload["summary"] == "cap 1234567 · alpha · 20 chars · text withheld"
    # The framing and the provenance are served as they were logged.
    assert (payload["item"]["address"], payload["item"]["length"], payload["freq_hz"]) == (
        1234567, 20, 152_840_000)
    assert log.read_bytes() == on_disk


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


# --- round 7 (Codex D-146 F1): a claim that waited on a restore keeps its deadline ------------


class _ConditionWaits:
    """Signals when a claim is inside the gate's Condition.wait, and with what timeout.

    The gate's own wait is wrapped on this one gate only; the implementation is unchanged. Once
    `entered` is set the claim has released the condition and is waiting, so the test can take
    the condition and be sure the claim is behind it."""

    def __init__(self, gate):
        self.entered = threading.Event()
        self.started = None
        self.timeout = None
        real = gate._state.wait

        def wait(timeout=None):
            if not self.entered.is_set():
                self.started, self.timeout = time.monotonic(), timeout
                self.entered.set()
            return real(timeout)

        gate._state.wait = wait


def _end_the_restore_late(gate, waits):
    """Once the claim is waiting, hold the gate's condition past that wait's own timeout, then end
    the restore under it: the claim's wait has timed out, and the gate is idle by the time it gets
    the lock back."""
    assert waits.entered.wait(JOIN_S), "the claim never waited on the restore"
    with gate._state:
        expiry = waits.started + (waits.timeout or 0.0)
        time.sleep(max(0.0, expiry - time.monotonic()) + 0.1)
        gate.end_restore()


def test_the_gate_refuses_a_claim_whose_wait_ran_out_even_if_the_restore_ended_meanwhile():
    # Codex D-146 F1: the condition wait accepted an idle gate when it got the lock back after its
    # timeout, so a claim that had waited out its whole budget went on to launch.
    gate = radio_control.TuningGate()
    gate.begin_restore(gate.now_ms())
    waits = _ConditionWaits(gate)
    until = time.monotonic() + 0.2
    outcome = {}

    def claim():
        try:
            gate.wait_idle(until=until)
            outcome["result"] = "admitted"
        except radio_control.TuningBusy as exc:
            outcome["result"] = str(exc)

    waiter = threading.Thread(target=claim, daemon=True)
    waiter.start()
    _end_the_restore_late(gate, waits)
    waiter.join(JOIN_S)
    assert not waiter.is_alive()
    assert outcome["result"] == radio_control.TUNING_REASON


def test_an_idle_gate_still_admits_a_claim_at_once():
    # No restore: nothing to wait for, so no deadline applies.
    gate = radio_control.TuningGate()
    gate.wait_idle(until=time.monotonic() - 1.0)


def test_a_claims_first_wait_on_a_restore_is_refused_at_its_deadline(tmp_path, monkeypatch):
    # Codex D-146 F1, through the route: the first APRS poll waited on a restore, the restore
    # ended just as the wait ran out, and the claim launched past its budget. It is refused, and
    # nothing starts.
    monkeypatch.setattr(radio_control, "TUNING_WAIT_S", 0.3)
    spawned = []
    packs = _radio_pack(tmp_path / "packs")
    with _client(tmp_path, probe=_present, aprs_factory=_aprs_factory(spawned),
                 sweeper_factory=_sweeper, pack_dir=packs) as client:
        gate = client.app.state.tuning_gate
        gate.begin_restore(gate.now_ms())
        waits = _ConditionWaits(gate)
        poll = _Request(lambda: client.get("/api/aprs"))
        poll.start()
        _end_the_restore_late(gate, waits)
        answer = poll.finish()
        assert answer.status_code == 503
        assert answer.json()["reason"] == radio_control.TUNING_REASON
        assert spawned == []
