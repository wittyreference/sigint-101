# ABOUTME: Integration tests for the FastAPI console — its routes and the server-side
# ABOUTME: commit-before-reveal gate, driven through the TestClient against a fixture pack.

import asyncio
import inspect
import io
import json
import os
import re
import urllib.error
import urllib.request
from contextlib import contextmanager
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101 import exam
from sigint101.acars import AcarsDecoder
from sigint101.adsb import AdsbDecoder
from sigint101.ais import AisDecoder
from sigint101.app import create_app
from sigint101.aprs import AprsDecoder
from sigint101.apt import AptDecoder
from sigint101.childproc import ChildWatch
from sigint101.decode import Decoder, parse_decode_line
from sigint101.dmr import DmrDecoder
from sigint101.events import log_event, read_events
from sigint101.flock import FlockDetector
from sigint101.hardware import RadioProbe, RtlSdrDevice
from sigint101.lora import LoRaListener, parse_lora_packet, radio_settings
from sigint101.pocsag import PocsagDecoder
from sigint101.provenance import BIAS_STATE, REFERENCE_HZ, read_baseline
from sigint101.rayhunter import RayhunterMonitor
from sigint101.rds import RDSDecoder, RDSStation, parse_rds_group
from sigint101.spectrum import (
    DEFAULT_BIN,
    DEFAULT_HIGH,
    DEFAULT_LOW,
    SpectrumUnavailable,
    Sweeper,
    SweepRow,
    validate_span,
)
from sigint101.sstv import SstvDecoder
from tests.test_spectrum import (
    FM_LINES,
    FailingPipeProc,
    FedProc,
    HeldProc,
    _feed_row,
    _flush_event,
    _fm_pass,
    wait_for_rows,
)

# The suite owns its lesson content: the real lesson_packs/ are authored in parallel, so a test
# that read them would go red on someone else's unfinished pack. These fixtures are stable.
FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"


def _present(detail="RTL2832U 0bda:2838 on USB bus"):
    """A probe result standing for a dongle physically on the bus."""
    return RadioProbe(True, "lsusb", detail)


def _absent(detail="no RTL2832U on USB bus"):
    """A probe result standing for an empty bus — the no-mock honest answer."""
    return RadioProbe(False, "lsusb", detail)


class _ChildStub:
    """What every decoder stand-in shares with the real decoders the routes read.

    A real ChildWatch that never sees a death, and no live command: the stand-ins run no process.
    """

    @property
    def watch(self):
        return self.__dict__.setdefault("_watch", ChildWatch("stub"))

    def live_command(self):
        return None


class _StubSweeper:
    """An app-boundary stand-in for the real rtl_power Sweeper.

    It holds real SweepRow objects and reports them as a running sweep would, so the spectrum and
    tune routes can be exercised with no dongle attached. This is a test double at the hardware
    boundary — never a fake baked into the product, which the no-mock invariant forbids.
    """

    def __init__(self, rows):
        self._rows = list(rows)
        self._started = False
        self.low, self.high, self.bin_size, self.gain = 88_000_000, 108_000_000, 125_000, None

    @property
    def running(self):
        return self._started

    def start(self):
        self._started = True

    def command(self):
        return ["rtl_power", "-f", f"{self.low}:{self.high}:{self.bin_size}"]

    def rows(self, limit=None):
        return self._rows if limit is None else self._rows[-limit:]

    def reading(self):
        # One run of rows, all swept under these settings, read while the stand-in "runs".
        row = self._rows[-1] if self._rows and self._started else None
        return row, {"serial": None, "gain": self.gain, "bin_size": self.bin_size,
                     "freq_low": self.low, "freq_high": self.high}

    def newest(self):
        # The stand-in's rows never age and it never stalls: its newest row is always a reading.
        row = self.reading()[0]
        return row, None, (None if row is None else 0.0)

    def retune(self, low, high, bin_size=None):
        self.low, self.high, self.bin_size = validate_span(low, high, bin_size)

    def release(self):
        self._started = False

    def resume(self):
        pass

    def stop(self):
        self._started = False


def _rows():
    """A couple of real full-band sweeps, so as_dict() (floor/peak/SNR) is exercised end to end."""
    return [
        SweepRow(ts="2026-01-01 00:00:00", freq_low=88_000_000.0, freq_high=108_000_000.0,
                 freq_step=5_000_000.0, dbm=(-40.0, -38.0, -12.0, -41.0, -39.0)),
        SweepRow(ts="2026-01-01 00:00:01", freq_low=88_000_000.0, freq_high=108_000_000.0,
                 freq_step=5_000_000.0, dbm=(-41.0, -37.0, -11.0, -42.0, -40.0)),
    ]


class _StubDecoder(_ChildStub):
    """An app-boundary stand-in for the real rtl_433 Decoder.

    Holds real Decode objects and reports them as a running decoder would, so the decode route
    can be exercised with no dongle attached. A test double at the hardware boundary, never a
    fake baked into the product.
    """

    def __init__(self, reads):
        self._reads = list(reads)
        self._started = False
        self._released = False
        self.freq_hz = 433_920_000

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        if self._released:
            raise AssertionError("start() after release without resume")
        self._started = True

    def command(self):
        return ["rtl_433", "-f", str(self.freq_hz), "-F", "json"]

    def recent(self, limit=None):
        return self._reads if limit is None else self._reads[-limit:]

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _reads():
    """A real ASK and a real FSK decode (FSK carries both tones), parsed from rtl_433 shapes."""
    ask = parse_decode_line(
        '{"time":"2026-01-01 00:00:00","model":"Acurite-Tower","id":12345,"channel":"A",'
        '"temperature_C":18.0,"humidity":51,"mod":"ASK","freq":433.95,"snr":36.3}')
    fsk = parse_decode_line(
        '{"time":"2026-01-01 00:00:01","model":"LaCrosse-TX141THBv2","id":252,'
        '"temperature_C":21.3,"humidity":45,"mod":"FSK","freq1":433.882,"freq2":433.958,"snr":22.4}')
    return [ask, fsk]


class _StubLora:
    """An app-boundary stand-in for the meshtasticd-backed LoRaListener (no real radio)."""

    def __init__(self, packets):
        self._packets = list(packets)
        self._started = False
        self.hostname, self.port = "localhost", 4403

    @property
    def running(self):
        return self._started

    def start(self):
        self._started = True

    def recent(self, limit=None):
        return self._packets if limit is None else self._packets[-limit:]

    def send_state(self):
        return {"radio": radio_settings(None), "sends": []}

    def stop(self):
        self._started = False


def _packets():
    """A real text packet heard below the noise floor and one above it, from meshtastic shapes."""
    below = parse_lora_packet({
        "fromId": "!433d1234", "from": 1128074276, "rxSnr": -11.25, "rxRssi": -119,
        "hopLimit": 3, "rxTime": 1730000000,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "ping from node B"}})
    above = parse_lora_packet({
        "fromId": "!433d1234", "from": 1128074276, "rxSnr": 6.5, "rxRssi": -92,
        "rxTime": 1730000005,
        "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {}}})
    return [below, above]


class _StubRDS(_ChildStub):
    """An app-boundary stand-in for the rtl_fm|redsea RDSDecoder (no real radio)."""

    def __init__(self, station):
        self._station = station
        self._started = False
        self._released = False
        self.freq_hz = 96_000_000

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        self._started = True

    def station(self):
        return self._station

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _station():
    """A real RDSStation assembled from real redsea group shapes."""
    s = RDSStation()
    s.absorb(parse_rds_group('{"pi":"0x6201","group":"0A","prog_type":"Serious classical"}'))
    s.absorb(parse_rds_group('{"pi":"0x6201","group":"0A","ps":"YLEKLAS1"}'))
    s.absorb(parse_rds_group('{"pi":"0x6201","group":"2A","radiotext":"Now playing: Finlandia"}'))
    return s


class _StubAdsb(_ChildStub):
    """An app-boundary stand-in for the dump1090 AdsbDecoder (no real radio)."""

    def __init__(self, aircraft, *, located=True):
        self._aircraft = aircraft
        self.receiver = type("_R", (), {"known": located})()
        self._started = False
        self._released = False

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        self._started = True

    def aircraft(self):
        return self._aircraft

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _aircraft():
    """One located aircraft, in the dict shape AdsbDecoder.aircraft() hands the route."""
    return [{
        "hex_ident": "A1B2C3", "callsign": "UAL123", "altitude_ft": 37000,
        "ground_speed_kt": 450.0, "track_deg": 118.0, "lat": 37.6195, "lon": -122.3868,
        "squawk": "2200", "message_count": 7, "range_nm": 12.4, "bearing_deg": 41.0,
    }]


class _StubAprs(_ChildStub):
    """An app-boundary stand-in for the rtl_fm|multimon-ng AprsDecoder (no real radio)."""

    def __init__(self, stations):
        self._stations = stations
        self._started = False
        self._released = False
        self.freq_hz = 144_390_000

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        self._started = True

    def stations(self):
        return self._stations

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _aprs_stations():
    """One decoded APRS station, in the dict shape AprsDecoder.stations() hands the route."""
    return [{
        "source": "KD2ABC-9", "dest": "APU25N", "path": ["WIDE1-1", "WIDE2-1"],
        "kind": "position", "lat": 40.70, "lon": -74.00, "text": "Test de KD2ABC",
        "addressee": None, "packet_count": 3,
    }]


class _StubPocsag(_ChildStub):
    """An app-boundary stand-in for the rtl_fm|multimon-ng PocsagDecoder (no real radio)."""

    def __init__(self, pages):
        self._pages = pages
        self._started = False
        self._released = False
        self.freq_hz = 929_612_500

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        self._started = True

    def pages(self, limit=None):
        return self._pages

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _pocsag_pages():
    """One decoded pager page, in the dict shape PocsagDecoder.pages() hands the route: framing
    only, the words withheld, as the real decoder serves every page it hears."""
    return [{
        "baud": 1200, "address": 1234567, "function": 3,
        "kind": "alpha", "text": None, "length": 20, "text_withheld": True,
    }]


class _StubAcars(_ChildStub):
    """An app-boundary stand-in for the acarsdec AcarsDecoder (no real radio)."""

    def __init__(self, messages):
        self._messages = messages
        self._started = False
        self._released = False
        self.freq_hz = 131_550_000

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        self._started = True

    def messages(self, limit=None):
        return self._messages

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _acars_messages():
    """One decoded ACARS message, in the dict shape AcarsDecoder.messages() hands the route."""
    return [{
        "tail": "F-GTAE", "flight": "AF7728", "label": "H1", "mode": "G",
        "block_id": "3", "msgno": "D65C", "ack": False, "text": "#DFB00000/V206",
        "freq_mhz": 131.55, "level": -8.5,
    }]


class _StubAis(_ChildStub):
    """An app-boundary stand-in for the rtl_ais AisDecoder (no real radio)."""

    def __init__(self, vessels):
        self._vessels = vessels
        self._started = False
        self._released = False

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        self._started = True

    def vessels(self, limit=None):
        return self._vessels

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _ais_vessels():
    """One decoded vessel, in the dict shape AisDecoder.vessels() hands the route."""
    return [{
        "mmsi": 351759000, "name": "EVER DIADEM", "lat": 47.58283, "lon": -122.34583,
        "sog_kt": 0.0, "cog_deg": 51.0, "heading_deg": 181, "nav_status": "Moored",
        "ship_type": 70, "message_count": 4, "last_seen": 2.0,
    }]


class _StubDmr(_ChildStub):
    """An app-boundary stand-in for the dsd-fme DmrDecoder (no real radio)."""

    def __init__(self, calls):
        self._calls = calls
        self._started = False
        self._released = False
        self.freq_hz = 450_000_000

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        self._started = True

    def calls(self, limit=None):
        return self._calls

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _dmr_calls():
    """One decoded call, in the dict shape DmrDecoder.calls() hands the route."""
    return [{
        "protocol": "P25", "talkgroup": 2001, "source": 1234567, "slot": None,
        "color_code": None, "kind": "group", "last_seen": "13:50:22",
    }]


class _StubApt(_ChildStub):
    """An app-boundary stand-in for the satdump AptDecoder (no real radio, no pass)."""

    def __init__(self, images):
        self._images = images
        self._started = False
        self._released = False
        self.freq_hz = 137_100_000

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        self._started = True

    def images(self, limit=None):
        return self._images

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _apt_images():
    """One decoded APT image, in the dict shape AptDecoder.images() hands the route."""
    return [{
        "path": "apt/NOAA-19/avhrr.png", "received_at": "2026-10-30T12:00:00+00:00",
        "satellite": "NOAA-19", "meta": {"filename": "avhrr.png", "size_bytes": 4096},
    }]


class _StubSstv(_ChildStub):
    """An app-boundary stand-in for the rtl_fm|sox|sstv SstvDecoder (no real radio, no picture)."""

    def __init__(self, images):
        self._images = images
        self._started = False
        self._released = False
        self.freq_hz = 145_800_000

    @property
    def running(self):
        return self._started and not self._released

    def start(self):
        self._started = True

    def images(self, limit=None):
        return self._images

    def release(self):
        self._released = True
        self._started = False

    def resume(self):
        self._released = False

    def stop(self):
        self._started = False


def _sstv_images():
    """One decoded SSTV picture, in the dict shape SstvDecoder.images() hands the route."""
    return [{
        "path": "sstv/20261030T120000-000.png", "received_at": 1793150400.0,
        "mode": "Martin 1", "meta": {"freq_hz": 145_800_000},
    }]


class _StubFlock(_ChildStub):
    """An app-boundary stand-in for the FlockDetector (no real wlan1 monitor radio)."""

    def __init__(self, detections):
        self._detections = detections
        self._started = False
        self.interface = "wlan1"

    @property
    def running(self):
        return self._started

    def start(self):
        self._started = True

    def detections(self, limit=None):
        return self._detections

    def stop(self):
        self._started = False


def _flock_detections():
    """One detected Flock device, in the dict shape FlockDetector.detections() hands the route."""
    return [{
        "mac": "b4:1e:52:00:00:01", "oui": "b4:1e:52", "tier": 4,
        "rule": "probe-request IE fingerprint", "ssid": None, "count": 3, "last_seen": 2.0,
    }]


class _StubRayhunter:
    """An app-boundary stand-in for the RayhunterMonitor (no real Orbic over WiFi)."""

    def __init__(self, status, warnings):
        self._status = status
        self._warnings = warnings
        self._started = False
        self.host, self.port = "192.168.1.1", 8080

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    @property
    def running(self):
        return self._started

    def start(self):
        self._started = True

    def status(self):
        return self._status

    def warnings(self, limit=None):
        return self._warnings

    def stop(self):
        self._started = False


def _rayhunter_warnings():
    """One heuristic finding, in the dict shape RayhunterMonitor.warnings() hands the route."""
    return [{
        "name": "IMSI Requested", "severity": "low", "packet": 5102,
        "description": "IMSI requested without authentication (packet 5102)",
        "timestamp": "2026-10-29T11:03:04-07:00",
    }]


class _StubFlipper:
    """An app-boundary stand-in for FlipperConsole (no real serial device)."""

    def __init__(self, device=None, error=None):
        self._device = device
        self._error = error

    def device(self):
        if self._error is not None:
            raise self._error
        return self._device


def _flipper_device():
    """The dict shape FlipperConsole.device() hands the route."""
    return {"model": "Flipper Zero", "name": "Ex4mple", "firmware": "1.4.3",
            "region": "US", "radio_alive": True}


def _client(tmp_path, *, probe=None, sweeper_factory=None, ppm_measure=None,
            decoder_factory=None, lora_factory=None, rds_factory=None, adsb_factory=None,
            aprs_factory=None, pocsag_factory=None, acars_factory=None, ais_factory=None,
            dmr_factory=None, apt_factory=None, sstv_factory=None, flock_factory=None,
            rayhunter_factory=None, flipper_factory=None, pool_path=None):
    """Build a console against the fixture pack and a temp data dir, with injectable hardware."""
    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path,
                     probe=probe or _absent, sweeper_factory=sweeper_factory,
                     ppm_measure=ppm_measure, decoder_factory=decoder_factory,
                     lora_factory=lora_factory, rds_factory=rds_factory,
                     adsb_factory=adsb_factory, aprs_factory=aprs_factory,
                     pocsag_factory=pocsag_factory, acars_factory=acars_factory,
                     ais_factory=ais_factory, dmr_factory=dmr_factory,
                     apt_factory=apt_factory, sstv_factory=sstv_factory,
                     flock_factory=flock_factory, rayhunter_factory=rayhunter_factory,
                     flipper_factory=flipper_factory, pool_path=pool_path)
    return TestClient(app)


# --- home ---------------------------------------------------------------------


def test_home_renders_a_tile_per_unit(tmp_path):
    """Home is the unit grid now: six unit tiles that drill into a unit's lessons.

    Lessons no longer sit directly on the home grid — they live one level down, under
    their unit — so a lesson title must not appear on home; a unit title and a /unit link
    must.
    """
    with _client(tmp_path) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "tile" in resp.text
    # The six curriculum units, by name and by link target.
    assert "Instrument" in resp.text
    assert "Modulation" in resp.text
    assert "Decode" in resp.text
    assert 'href="/unit/1"' in resp.text
    assert 'href="/unit/6"' in resp.text
    # A lesson title lives under its unit, not on the home grid.
    assert "Fixture Gain and SNR" not in resp.text


def test_unit_page_lists_its_lessons(tmp_path):
    """A unit page shows that unit's loaded lessons, each linking to the lesson."""
    with _client(tmp_path) as client:
        resp = client.get("/unit/1")
    assert resp.status_code == 200
    assert "Instrument" in resp.text
    assert "Fixture Gain and SNR" in resp.text
    assert "Fixture Second Lesson" in resp.text
    assert 'href="/lesson/1"' in resp.text
    assert 'href="/lesson/2"' in resp.text


def _write_pack(pack_dir, lesson_id, *, hardware=("rtl-sdr",), title=None, intro=False):
    """Write a minimal pack, for tests that need a lesson id or shape the fixtures lack.

    Consolidation by default; `intro` writes the six-stage shape, whose frame/demo/try open
    before the predict gate. Each stage body carries a STAGE_<name> marker to find it by.
    """
    hw = ", ".join(hardware)
    stages = (("frame", "demo", "try", "predict", "observe", "explain") if intro
              else ("predict", "operate", "observe", "explain"))
    body = "".join(f"  {s}: STAGE_{s} of lesson {lesson_id}\n" for s in stages)
    (pack_dir / f"{lesson_id:02d}-tmp.yaml").write_text(
        f"id: {lesson_id}\nslug: tmp-{lesson_id}\ntitle: {title or f'Tmp Lesson {lesson_id}'}\n"
        f"concept: tmp concept {lesson_id}\ntype: {'intro' if intro else 'consolidation'}\n"
        f"hardware: [{hw}]\nstages:\n{body}",
        encoding="utf-8")


def _client_with_packs(tmp_path, *packs):
    """A console over a temp pack dir holding just the given (lesson_id, kwargs) packs."""
    pack_dir = tmp_path / "packs"
    pack_dir.mkdir()
    for lesson_id, kwargs in packs:
        _write_pack(pack_dir, lesson_id, **kwargs)
    return TestClient(create_app(pack_dir=pack_dir, data_dir=tmp_path, probe=_absent))


def test_a_lesson_tile_shows_its_teaching_order_code_not_its_id(tmp_path):
    """AIS is id 26 but the 17th lesson taught, so its tile reads L17; the id stays the URL."""
    with _client_with_packs(tmp_path, (13, {}), (26, {})) as client:
        html = client.get("/unit/5").text
    assert re.search(r'tile__code">\s*L13\s*<', html)
    assert re.search(r'tile__code">\s*L17\s*<', html)
    assert "L26" not in html
    assert 'href="/lesson/26"' in html


def test_a_lesson_tile_badges_the_hardware_it_needs_beyond_the_radio(tmp_path):
    """A need shows on the tile before the lesson starts; the built-in RTL-SDR earns no badge."""
    with _client_with_packs(tmp_path, (17, {"hardware": []}),
                            (18, {"hardware": ["wifi-monitor"]}),
                            (19, {"hardware": ["orbic-rayhunter"]})) as client:
        html = client.get("/unit/7").text
    assert re.findall(r'tile__need">\s*([^<]*?)\s*<', html) == ["Wi-Fi monitor", "Orbic"]


def test_a_radio_only_lesson_tile_carries_no_badge(tmp_path):
    with _client_with_packs(tmp_path, (13, {"hardware": ["rtl-sdr"]})) as client:
        html = client.get("/unit/5").text
    assert "tile__need" not in html


def test_unit_page_for_an_unknown_unit_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/unit/99")
    assert resp.status_code == 404


def test_unit_page_with_no_lessons_yet_is_honest(tmp_path):
    """A real unit with nothing built yet renders (no lock, D16) and says so plainly."""
    with _client(tmp_path) as client:
        resp = client.get("/unit/5")  # Decode — no fixture packs load into it
    assert resp.status_code == 200
    assert "Decode" in resp.text
    assert 'href="/lesson/' not in resp.text
    assert "No lessons here yet" in resp.text


def test_home_unit_tile_shows_aggregate_lesson_progress(tmp_path):
    """Unit 1 holds two loaded fixture lessons; finishing one reads as 1 of 2 done."""
    with _client(tmp_path) as client:
        before = client.get("/").text
        assert "0/2" in before  # Unit 1: two loaded lessons, none done
        client.post("/api/predict?lesson_id=1", data={"prediction": "a"},
                    headers={"content-type": "application/x-www-form-urlencoded"})
        client.post("/api/complete?lesson_id=1", data={},
                    headers={"content-type": "application/x-www-form-urlencoded"})
        after = client.get("/").text
    assert "1/2" in after


def test_home_links_to_the_exam_trainer(tmp_path):
    """The licence exam trainer is reachable from home, not only by typing the URL.

    It is a cross-cutting track, not one of the six curriculum units, so it sits in its
    own affordance (not a unit tile) that lands on /exam.
    """
    with _client(tmp_path) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/exam"' in resp.text
    assert "exam-cta" in resp.text          # its own affordance, not a unit tile
    assert "Licence" in resp.text


# --- commit-before-reveal (the whole point, enforced server-side) -------------


def test_a_lesson_seals_every_post_predict_stage_before_a_commit(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/lesson/1")
    assert resp.status_code == 200
    # The predict gate is shown...
    assert "What happens to the gap" in resp.text
    # ...but nothing past it is in the HTML at all — not merely hidden, absent.
    assert "OPERATE_SENTINEL" not in resp.text
    assert "OBSERVE_SENTINEL" not in resp.text
    assert "EXPLAIN_SENTINEL" not in resp.text


def test_recording_a_prediction_unseals_the_rest_of_the_lesson(tmp_path):
    with _client(tmp_path) as client:
        posted = client.post("/api/predict?lesson_id=1", data={"prediction": "a"},
                             follow_redirects=False)
        assert posted.status_code == 303
        resp = client.get("/lesson/1")
    assert "OPERATE_SENTINEL" in resp.text
    assert "OBSERVE_SENTINEL" in resp.text
    assert "EXPLAIN_SENTINEL" in resp.text


def test_a_blank_prediction_is_refused_fail_closed(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/predict?lesson_id=1", data={"prediction": "   "})
    assert resp.status_code == 400
    assert resp.json()["recorded"] is False
    assert "blank" in resp.json()["reason"]


def test_a_prediction_accepts_json_too(tmp_path):
    with _client(tmp_path) as client:
        posted = client.post("/api/predict?lesson_id=1", json={"prediction": "b"},
                             follow_redirects=False)
        assert posted.status_code == 303
        resp = client.get("/lesson/1")
    assert "OPERATE_SENTINEL" in resp.text


def test_a_committed_prediction_is_scored_against_the_pack_answer(tmp_path):
    with _client(tmp_path) as client:
        client.post("/api/predict?lesson_id=1", data={"prediction": "c"}, follow_redirects=False)
        client.post("/api/predict?lesson_id=2", data={"prediction": "a"}, follow_redirects=False)
        progress = client.app.state.progress
        assert progress.get_correct(1) is True
        # Fixture 2 names no answer, so it is stored unscored rather than guessed wrong.
        assert progress.get_correct(2) is None


def test_a_second_commit_is_refused_and_the_first_prediction_stands(tmp_path):
    with _client(tmp_path) as client:
        first = client.post("/api/predict?lesson_id=1", data={"prediction": "a"},
                            follow_redirects=False)
        assert first.status_code == 303
        second = client.post("/api/predict?lesson_id=1", data={"prediction": "c"},
                             follow_redirects=False)
        assert second.status_code == 409
        assert second.json()["recorded"] is False
        assert "already" in second.json()["reason"]
        progress = client.app.state.progress
        assert progress.get_prediction(1) == "a"
        assert progress.get_correct(1) is False


def test_the_explain_header_shows_the_scored_call_after_a_commit(tmp_path):
    with _client(tmp_path) as client:
        sealed = client.get("/lesson/1").text
        assert "Your call:" not in sealed
        assert "VERDICT_SENTINEL" not in sealed
        client.post("/api/predict?lesson_id=1", data={"prediction": "a"}, follow_redirects=False)
        html = client.get("/lesson/1").text
    assert "Your call: <b>The gap grows</b>" in html
    # The fixture lesson needs no radio, so its answer reads as a fact.
    assert "In fact: <b>The gap shrinks past a point</b>" in html
    # The repeated verdict line is dropped on explain; the explain body already says why.
    assert "VERDICT_SENTINEL" not in html
    assert "MISSED" not in html


def test_predict_on_an_unknown_lesson_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/predict?lesson_id=999", data={"prediction": "a"})
    assert resp.status_code == 404


def test_opening_an_unknown_lesson_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/lesson/999")
    assert resp.status_code == 404


# --- error pages: a browser gets a themed page, never raw JSON or a white sheet ----------------

_BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def test_an_unknown_lesson_is_a_themed_page_for_the_kiosk_browser(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/lesson/999", headers=_BROWSER)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    # The console's own shell, so it takes the learner's theme (red-night stays red) and keeps Back.
    assert 'class="app"' in resp.text and "btn-back" in resp.text
    assert "lesson 999 not found" in resp.text
    assert 'href="/"' in resp.text


def test_an_unknown_path_is_a_themed_page_for_the_kiosk_browser(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/no/such/screen", headers=_BROWSER)
    assert resp.status_code == 404
    assert 'class="app"' in resp.text


def test_api_errors_stay_json_even_for_a_browser(tmp_path):
    # The panels' fetch() calls read the JSON reason; an HTML page there would break them.
    with _client(tmp_path) as client:
        resp = client.post("/api/complete?lesson_id=999", headers=_BROWSER)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "lesson 999 not found"


def test_a_client_that_does_not_ask_for_html_still_gets_json(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/lesson/999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "lesson 999 not found"


# --- complete -----------------------------------------------------------------


def test_complete_is_refused_without_a_prediction(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/complete?lesson_id=1")
    assert resp.status_code == 400
    assert resp.json()["completed"] is False
    assert "predict" in resp.json()["reason"]


def test_complete_succeeds_after_a_prediction(tmp_path):
    with _client(tmp_path) as client:
        client.post("/api/predict?lesson_id=1", data={"prediction": "b"},
                    follow_redirects=False)
        resp = client.post("/api/complete?lesson_id=1")
    assert resp.status_code == 200
    assert resp.json()["completed"] is True


def test_complete_on_an_unknown_lesson_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/complete?lesson_id=999")
    assert resp.status_code == 404


def _complete_href(html):
    """Where the Complete button sends the learner once the lesson is recorded."""
    return re.search(r'data-complete[^>]*data-next-href="([^"]*)"', html).group(1)


def test_completing_a_lesson_leads_on_to_the_next_one_in_its_unit(tmp_path):
    """Finishing L1 lands on L2, not on home three taps away from it."""
    with _client(tmp_path) as client:
        client.post("/api/predict?lesson_id=1", data={"prediction": "a"})
        html = client.get("/lesson/1").text
    assert _complete_href(html) == "/lesson/2"


def test_completing_the_last_lesson_of_a_unit_returns_to_the_unit(tmp_path):
    """Lesson 2 is the last loaded lesson of Unit 1 in the fixtures, so it lands on /unit/1."""
    with _client(tmp_path) as client:
        client.post("/api/predict?lesson_id=2", data={"prediction": "a"})
        html = client.get("/lesson/2").text
    assert _complete_href(html) == "/unit/1"


def test_the_next_lesson_skips_ids_that_have_no_pack_yet(tmp_path):
    """Unit 5 holds 13-16, 26, 27; with only 13 and 26 built, 13 leads straight to 26."""
    with _client_with_packs(tmp_path, (13, {}), (26, {})) as client:
        client.post("/api/predict?lesson_id=13", data={"prediction": "a"})
        html = client.get("/lesson/13").text
    assert _complete_href(html) == "/lesson/26"


def test_a_lessons_back_goes_to_its_unit_not_home(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/lesson/1").text
    assert re.search(r'class="btn-back" href="/unit/1"', html)
    assert "Unit 1" in html


def test_a_lesson_outside_every_unit_falls_back_to_home(tmp_path):
    """No unit to return to or continue in, so Back and Complete both lead home."""
    with _client_with_packs(tmp_path, (99, {})) as client:
        client.post("/api/predict?lesson_id=99", data={"prediction": "a"})
        html = client.get("/lesson/99").text
    assert re.search(r'class="btn-back" href="/"', html)
    assert _complete_href(html) == "/"


# --- where the learner is inside a lesson (resume, two-way navigation) -------


def _shown_stage(html):
    """The one stage panel rendered without `hidden`: where the lesson opens."""
    return re.search(r'<section class="stage-panel[^"]*" data-stage="(\w+)">', html).group(1)


def _stage(client, lesson_id, stage):
    return client.post(f"/api/stage?lesson_id={lesson_id}", data={"stage": stage},
                       headers={"content-type": "application/x-www-form-urlencoded"})


def test_showing_a_lesson_page_leaves_its_opened_state_unchanged(tmp_path):
    # A GET has no side effect: a render check, a prefetch or a crawler that loads a lesson must
    # not mark it opened. The page records the open itself (lesson-nav.js, POST /api/opened).
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        assert client.get("/lesson/1").status_code == 200
        assert client.app.state.progress.get(1) is None


def test_the_lesson_page_posting_its_open_marks_the_lesson_opened(tmp_path):
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        resp = client.post("/api/opened?lesson_id=1")
        assert resp.status_code == 200
        assert resp.json() == {"opened": True, "lesson_id": 1}
        row = client.app.state.progress.get(1)
        assert row["opened"] == 1 and row["completed"] == 0


def test_an_open_of_a_lesson_the_course_lacks_is_refused_and_records_nothing(tmp_path):
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        assert client.post("/api/opened?lesson_id=99").status_code == 404
        assert client.app.state.progress.get(99) is None


RACK_APRS_HZ = 144_800_000   # where the rack's tune keys left the 2 m decoder
RACK_ISM_HZ = 434_000_000    # and the ISM decoder


def _tuned_away(client):
    """Leave two decoders where the rack retuned them, away from their lesson frequency."""
    client.app.state.aprs.retune(RACK_APRS_HZ)
    client.app.state.decoder.retune(RACK_ISM_HZ)
    return client.app.state.aprs.home_hz, client.app.state.decoder.home_hz


def test_showing_a_lesson_page_changes_no_receiver(tmp_path):
    # A GET has no side effect: loading a lesson (a render check, a prefetch) must not retune a
    # receiver, which can stop a running one and drop what it had decoded.
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        _tuned_away(client)
        assert client.get("/lesson/1").status_code == 200
        assert client.app.state.aprs.freq_hz == RACK_APRS_HZ
        assert client.app.state.decoder.freq_hz == RACK_ISM_HZ


def test_the_lesson_page_posting_its_tuning_puts_the_receivers_on_the_lessons_frequencies(
        tmp_path):
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        aprs_home, ism_home = _tuned_away(client)
        resp = client.post("/api/tuning?lesson_id=1")
        assert resp.status_code == 200
        assert resp.json() == {"tuned": True, "lesson_id": 1}
        assert client.app.state.aprs.freq_hz == aprs_home
        assert client.app.state.decoder.freq_hz == ism_home


def test_a_tuning_post_for_a_lesson_the_course_lacks_is_refused_and_changes_nothing(tmp_path):
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        _tuned_away(client)
        assert client.post("/api/tuning?lesson_id=99").status_code == 404
        assert client.app.state.aprs.freq_hz == RACK_APRS_HZ


def test_the_lesson_page_restores_its_tuning_before_any_panel_starts_polling(tmp_path):
    # lesson-tuning.js sets up the gate the panels' first polls wait on, so it must load ahead of
    # every panel script.
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        html = client.get("/lesson/1").text
    tag = re.search(r'<script src="/static/lesson-tuning\.js"([^>]*)>', html)
    assert tag, "the lesson page does not load lesson-tuning.js"
    assert 'data-lesson-id="1"' in tag.group(1)
    first_panel = min(html.index(f'src="/static/{name}.js"')
                      for name in ("decoder-panel", "waterfall", "decode", "aprs"))
    assert tag.start() < first_panel


def test_the_lesson_page_has_a_note_that_says_when_its_tuning_is_late_or_refused(tmp_path):
    # lesson-tuning.js writes into it once the panels stop waiting for a slow restore, or when the
    # restore is refused. Hidden until then: a restore that worked has nothing to say.
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        html = client.get("/lesson/1").text
    note = re.search(r"<[a-z]+[^>]*\bdata-lesson-tuning\b[^>]*>", html)
    assert note, "the lesson page has no tuning note for lesson-tuning.js to fill"
    assert 'role="status"' in note.group(0)
    assert re.search(r"\shidden\b", note.group(0))


def test_the_rack_never_restores_a_lessons_tuning(tmp_path):
    # The rack's polls never wait on, or apply, a lesson's tuning: it loads no lesson-tuning.js.
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        html = client.get("/operate/ism").text
    assert 'src="/static/decode.js"' in html
    assert "lesson-tuning" not in html


# Every page the render checks load (tools/render/*.py), in the order make render-check runs them.
# Every page the render checks load (tools/render/*.py), in the order make render-check runs them.
# test_every_page_a_render_check_loads_is_in_the_walk keeps this list whole.
RENDER_CHECK_PAGES = ("/lesson/29", "/", "/exam/review/T6C02", "/operate/ism", "/logbook",
                      "/operate/lora", "/lesson/5", "/law?back=/lesson/5", "/lesson/39",
                      "/lesson/1", "/lesson/1?recall=card&card=2", "/unit/4", "/unit/5",
                      "/unit/4/challenge", "/unit/5/challenge",
                      "/operate/ism?back=/unit/4/challenge")


def test_every_page_a_render_check_loads_is_in_the_walk():
    # A page a render tool loads (f"{console}/..." or f"{base}/...") must be walked below, so a
    # GET that starts writing fails here. A {placeholder} matches any one path segment.
    tools = Path(__file__).resolve().parent.parent / "tools" / "render"
    found = set()
    for path in tools.glob("*.py"):
        found |= set(re.findall(r'f"\{(?:console|base)\}(/[^"]*)"', path.read_text("utf-8")))
    assert found, "no page paths found in tools/render"
    for page in sorted(found):
        pattern = re.sub(r"\\\{[^}]*\\\}", "[^/?&]+", re.escape(page))
        assert any(re.fullmatch(pattern, walked) for walked in RENDER_CHECK_PAGES), page


def _stored_state(data_dir):
    """Every row of the console's two databases, and the event log's bytes."""
    import sqlite3
    from contextlib import closing
    state = {}
    for name in ("progress.db", "exam.db"):
        path = data_dir / name
        if path.exists():
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
                state[name] = list(db.iterdump())
    events = data_dir / "events.jsonl"
    state["events.jsonl"] = events.read_bytes() if events.exists() else None
    return state


def _check_lesson(app):
    """A lesson with a check (a retrieval source once completed), and one choice of its predict."""
    lesson = next(item for _, item in sorted(app.state.lessons.items()) if item.check)
    return lesson.id, lesson.choices[0]["key"]


def test_a_render_check_walk_against_a_live_console_changes_none_of_its_state(tmp_path):
    # With /api/* blocked, a check's page loads are plain GETs. Against a console whose rack has
    # retuned two decoders and holds a place, and with a completed check lesson that has no
    # retrieval row yet, none of them may move a receiver or write a row or an event: the next
    # GET that writes fails here.
    app = create_app(data_dir=tmp_path, probe=_absent)
    with TestClient(app) as client:
        _tuned_away(client)
        client.post("/api/rack/place", json={"instrument": "pager"})
        source, _ = _check_lesson(app)
        app.state.progress.mark_completed(source)
        assert app.state.progress.get_retrieval([source]) == {}
        before = _stored_state(tmp_path)
        for page in RENDER_CHECK_PAGES:
            assert client.get(page).status_code == 200, page
        assert app.state.aprs.freq_hz == RACK_APRS_HZ
        assert app.state.decoder.freq_hz == RACK_ISM_HZ
        assert app.state.progress.get_retrieval([source]) == {}
        assert _stored_state(tmp_path) == before


def test_a_lesson_get_enrols_no_completed_check_lesson_for_recall(tmp_path):
    app = create_app(data_dir=tmp_path, probe=_absent)
    with TestClient(app) as client:
        source, _ = _check_lesson(app)
        app.state.progress.mark_completed(source)
        other = next(lid for lid in sorted(app.state.lessons) if lid != source)
        assert client.get(f"/lesson/{other}").status_code == 200
        assert app.state.progress.get_retrieval([source]) == {}


def test_completing_a_check_lesson_enrols_it_for_recall(tmp_path):
    app = create_app(data_dir=tmp_path, probe=_absent)
    with TestClient(app) as client:
        source, choice = _check_lesson(app)
        client.post(f"/api/predict?lesson_id={source}", data={"prediction": choice},
                    follow_redirects=False)
        assert client.post(f"/api/complete?lesson_id={source}").status_code == 200
        row = app.state.progress.get_retrieval([source])[source]
        assert row["box"] == 1 and row["seen"] == 0


def test_a_completion_without_a_check_enrols_nothing(tmp_path):
    app = create_app(data_dir=tmp_path, probe=_absent)
    with TestClient(app) as client:
        plain = next(item for _, item in sorted(app.state.lessons.items()) if not item.check)
        client.post(f"/api/predict?lesson_id={plain.id}",
                    data={"prediction": plain.choices[0]["key"]}, follow_redirects=False)
        assert client.post(f"/api/complete?lesson_id={plain.id}").status_code == 200
        assert app.state.progress.get_retrieval([plain.id]) == {}


def test_completions_from_before_recall_are_enrolled_when_the_console_starts(tmp_path):
    # A check lesson completed before enrolment moved to the completion post has no row; the
    # console enrols it once, at startup, beside the stamp backfill, never on a page load.
    from sigint101.app import PACK_DIR
    from sigint101.lessons import load_lessons
    from sigint101.progress import Progress
    source = next(lid for lid, item in sorted(load_lessons(PACK_DIR).items()) if item.check)
    plain = next(lid for lid, item in sorted(load_lessons(PACK_DIR).items()) if not item.check)
    seeded = Progress(tmp_path / "progress.db")
    seeded.mark_completed(plain)
    seeded.mark_completed(source)
    assert seeded.get_retrieval([source]) == {}
    seeded.close()
    with TestClient(create_app(data_dir=tmp_path, probe=_absent)) as client:
        assert source in client.app.state.progress.get_retrieval([source])
        assert client.app.state.progress.get_retrieval([plain]) == {}


def test_reopening_a_lesson_resumes_the_stage_the_learner_left(tmp_path):
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        assert _shown_stage(client.get("/lesson/1").text) == "frame"
        resp = _stage(client, 1, "try")
        assert resp.status_code == 200
        assert resp.json() == {"recorded": True, "stage": "try", "furthest": 2}
        assert _shown_stage(client.get("/lesson/1").text) == "try"


def test_committing_a_prediction_moves_the_learner_on_to_the_first_sealed_stage(tmp_path):
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        _stage(client, 1, "predict")
        client.post("/api/predict?lesson_id=1", data={"prediction": "a"})
        html = client.get("/lesson/1").text
    assert _shown_stage(html) == "observe"


def test_after_a_commit_the_learner_may_go_back_and_resume_there(tmp_path):
    """Revisiting the setup after the reveal is allowed, and a reload keeps them on it."""
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        client.post("/api/predict?lesson_id=1", data={"prediction": "a"})
        _stage(client, 1, "explain")
        assert _stage(client, 1, "frame").json()["furthest"] == 5
        html = client.get("/lesson/1").text
    assert _shown_stage(html) == "frame"
    assert "STAGE_explain" in html          # the reveal stays unsealed after going back


def test_a_sealed_stage_cannot_be_recorded_before_the_commit(tmp_path):
    """Commit-before-reveal: no stage past predict can be reached, or resumed, without a guess."""
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        resp = _stage(client, 1, "observe")
        assert resp.status_code == 400
        assert resp.json()["recorded"] is False
        html = client.get("/lesson/1").text
    assert _shown_stage(html) == "frame"
    assert "STAGE_observe" not in html


def test_a_stage_the_lesson_does_not_have_is_refused(tmp_path):
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        assert _stage(client, 1, "operate").status_code == 400    # an intro pack has no operate
        assert _stage(client, 1, "").status_code == 400


def test_recording_a_stage_on_an_unknown_lesson_is_404(tmp_path):
    with _client(tmp_path) as client:
        assert _stage(client, 999, "predict").status_code == 404


def _pip(html, stage):
    """The markup for one stage in the progress rail."""
    return re.search(rf'<\w+ class="rail-pip[^"]*"[^>]*data-stage="{stage}"[^>]*>',
                     html).group(0)


def test_the_rail_reports_each_stage_state_without_jump_targets(tmp_path):
    with _client_with_packs(tmp_path, (1, {"intro": True})) as client:
        _stage(client, 1, "try")
        _stage(client, 1, "demo")                 # stepped back: try was still reached
        html = client.get("/lesson/1").text
    assert 'data-nav="goto"' not in html
    expected = {"frame": "done", "demo": "active", "try": "done",
                "predict": "ahead", "observe": "locked", "explain": "locked"}
    for stage, state in expected.items():
        pip = _pip(html, stage)
        assert pip.startswith("<li ") and "disabled" not in pip
        assert f"rail-pip--{state}" in pip
        assert ('aria-current="step"' in pip) == (stage == "demo")


# --- spectrum (no-mock: absence is an answer, never a fake row) ---------------


def test_spectrum_reports_503_and_a_reason_when_the_sweeper_sees_no_radio(tmp_path):
    with _client(tmp_path,
                 sweeper_factory=lambda: Sweeper(radio_present=lambda: False)) as client:
        resp = client.get("/api/spectrum")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["rows"] == []        # never a fabricated sweep
    assert "RTL-SDR" in body["reason"]


def test_spectrum_default_sweeper_also_refuses_without_a_radio(tmp_path):
    # No sweeper_factory injected: the real build_sweeper runs and must still refuse, not fake.
    with _client(tmp_path, probe=_absent) as client:
        resp = client.get("/api/spectrum")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_spectrum_returns_real_rows_when_the_sweep_is_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.get("/api/spectrum")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_low"] == 88_000_000
    assert len(body["rows"]) == 2
    assert body["rows"][0]["dbm"] == [-40.0, -38.0, -12.0, -41.0, -39.0]
    assert "snr_db" in body["rows"][0]


# --- decode (rtl_433; no-mock: absence is an answer, never a fake read) -------


def test_decode_reports_503_and_a_reason_when_no_radio(tmp_path):
    # rtl_433 present (injected which) so this pins the no-radio refusal, not the no-binary one.
    with _client(tmp_path,
                 decoder_factory=lambda: Decoder(radio_present=lambda: False,
                                                 which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/decode")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["decodes"] == []         # never a fabricated read
    assert "RTL-SDR" in body["reason"]


def test_decode_reports_503_when_rtl_433_is_missing(tmp_path):
    with _client(tmp_path,
                 decoder_factory=lambda: Decoder(radio_present=lambda: True,
                                                 which=lambda name: None)) as client:
        resp = client.get("/api/decode")
    assert resp.status_code == 503
    assert "rtl_433" in resp.json()["reason"]


def test_decode_default_decoder_also_refuses_without_a_radio(tmp_path):
    # No decoder_factory injected: the real build_decoder runs and must still refuse, not fake.
    with _client(tmp_path, probe=_absent) as client:
        resp = client.get("/api/decode")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_decode_returns_real_reads_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 decoder_factory=lambda: _StubDecoder(_reads())) as client:
        resp = client.get("/api/decode")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_hz"] == 433_920_000
    assert [d["model"] for d in body["decodes"]] == ["Acurite-Tower", "LaCrosse-TX141THBv2"]
    # The FSK read carries both tones — the shape M10 is about.
    fsk = body["decodes"][1]
    assert fsk["mod"] == "FSK"
    assert fsk["freq_hz"] == 433_882_000 and fsk["freq2_hz"] == 433_958_000


def test_decode_stands_the_sweeper_down(tmp_path):
    """Starting the decoder releases the dongle from the sweeper — one program at a time."""
    released = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def release(self):
            released["value"] = True
            self.stop()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 decoder_factory=lambda: _StubDecoder(_reads())) as client:
        client.get("/api/decode")
    assert released["value"] is True


def test_spectrum_reclaims_the_dongle_from_the_decoder(tmp_path):
    """Switching back to the waterfall releases the decoder and resumes the sweep."""
    resumed = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def resume(self):
            resumed["value"] = True

    released = {"value": False}

    class _RecordingDecoder(_StubDecoder):
        def release(self):
            released["value"] = True
            super().release()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 decoder_factory=lambda: _RecordingDecoder(_reads())) as client:
        client.get("/api/decode")       # decoder takes the dongle
        client.get("/api/spectrum")     # waterfall reclaims it
    assert released["value"] is True
    assert resumed["value"] is True


# The seven consumers that share the one RTL-SDR; the arbitration contract names them all.
_SDR_CONSUMERS = ("sweeper", "decoder", "rds", "adsb", "aprs", "pocsag", "acars", "ais", "dmr",
                  "apt", "sstv")


def _recording_sdr_client(tmp_path):
    """A client whose seven SDR consumers record every release/resume, plus untouched LoRa/Flock.

    The RTL-SDR is single-tenant: claiming it for one route must resume that consumer and release
    the other ten, while the separate-radio LoRa (meshtasticd) and Flock (wlan1 monitor) consumers
    are never disturbed. Returns the client and a {"release", "resume"} ledger of consumer names.
    """
    calls = {"release": [], "resume": []}

    def recorder(cls, name, *data):
        class _Rec(cls):
            def release(self):
                calls["release"].append(name)
                super().release()

            def resume(self):
                calls["resume"].append(name)
                super().resume()

        return _Rec(*data)

    client = _client(
        tmp_path, probe=_present,
        sweeper_factory=lambda: recorder(_StubSweeper, "sweeper", _rows()),
        decoder_factory=lambda: recorder(_StubDecoder, "decoder", _reads()),
        rds_factory=lambda: recorder(_StubRDS, "rds", _station()),
        adsb_factory=lambda: recorder(_StubAdsb, "adsb", _aircraft()),
        aprs_factory=lambda: recorder(_StubAprs, "aprs", _aprs_stations()),
        pocsag_factory=lambda: recorder(_StubPocsag, "pocsag", _pocsag_pages()),
        acars_factory=lambda: recorder(_StubAcars, "acars", _acars_messages()),
        ais_factory=lambda: recorder(_StubAis, "ais", _ais_vessels()),
        dmr_factory=lambda: recorder(_StubDmr, "dmr", _dmr_calls()),
        apt_factory=lambda: recorder(_StubApt, "apt", _apt_images()),
        sstv_factory=lambda: recorder(_StubSstv, "sstv", _sstv_images()),
        lora_factory=lambda: _StubLora(_packets()),
        flock_factory=lambda: _StubFlock(_flock_detections()),
    )
    return client, calls


@pytest.mark.parametrize("route,target", [
    ("/api/spectrum", "sweeper"),
    ("/api/decode", "decoder"),
    ("/api/rds", "rds"),
    ("/api/adsb", "adsb"),
    ("/api/aprs", "aprs"),
    ("/api/pocsag", "pocsag"),
    ("/api/acars", "acars"),
    ("/api/ais", "ais"),
    ("/api/dmr", "dmr"),
    ("/api/apt", "apt"),
    ("/api/sstv", "sstv"),
])
def test_claiming_an_sdr_route_stands_down_every_other_sdr_consumer(tmp_path, route, target):
    """One RTL-SDR, one tenant: a route resumes its own consumer and releases the other ten.

    The separate-radio consumers (LoRa, Flock) are never touched. This pins the claim()
    arbitration so a Decode+ consumer can be added to the set without hand-editing every rival's
    release list — the exact per-route edit this contract replaces.
    """
    client, calls = _recording_sdr_client(tmp_path)
    with client:
        resp = client.get(route)
    assert resp.status_code == 200
    others = set(_SDR_CONSUMERS) - {target}
    assert target in calls["resume"]
    assert set(calls["release"]) == others
    assert target not in calls["release"]


# --- lora (meshtasticd; no-mock: absence is an answer, never a fake packet) ---


def test_lora_reports_503_and_a_reason_when_meshtasticd_is_unreachable(tmp_path):
    def refuse(host, port):
        raise OSError("connection refused")
    with _client(tmp_path,
                 lora_factory=lambda: LoRaListener(connect=refuse)) as client:
        resp = client.get("/api/lora")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["packets"] == []          # never a fabricated reception
    assert "LoRa" in body["reason"] or "meshtasticd" in body["reason"]


def test_lora_default_listener_also_refuses_without_a_daemon(tmp_path):
    # No lora_factory injected: the real build_lora runs and must still refuse (no meshtasticd).
    with _client(tmp_path) as client:
        resp = client.get("/api/lora")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_lora_returns_real_packets_with_their_snr(tmp_path):
    with _client(tmp_path, lora_factory=lambda: _StubLora(_packets())) as client:
        resp = client.get("/api/lora")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    # The first packet decoded at a NEGATIVE SNR — below the noise floor, the lesson's point.
    first = body["packets"][0]
    assert first["from_id"] == "!433d1234"
    assert first["snr_db"] < 0 and first["below_floor"] is True
    assert "ping from node B" in first["summary"]


# --- rds (rtl_fm|redsea; no-mock: absence is an answer, never a fake station) -


def test_rds_reports_503_and_a_reason_when_no_radio(tmp_path):
    # redsea+rtl_fm present (injected which) so this pins the no-radio refusal.
    with _client(tmp_path,
                 rds_factory=lambda: RDSDecoder(radio_present=lambda: False,
                                                which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/rds")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["station"] is None        # never a fabricated station
    assert "RTL-SDR" in body["reason"]


def test_rds_default_decoder_also_refuses_without_a_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/rds")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_rds_returns_the_assembled_station_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 rds_factory=lambda: _StubRDS(_station())) as client:
        resp = client.get("/api/rds")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_hz"] == 96_000_000
    st = body["station"]
    assert st["pi"] == "0x6201"
    assert st["ps"] == "YLEKLAS1"                       # the hidden station name
    assert "Finlandia" in st["radiotext"]
    assert st["group_count"] == 3


def test_rds_stands_the_sweeper_down(tmp_path):
    released = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def release(self):
            released["value"] = True
            self.stop()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 rds_factory=lambda: _StubRDS(_station())) as client:
        client.get("/api/rds")
    assert released["value"] is True


# --- adsb (ADS-B radar, M13) --------------------------------------------------


def test_adsb_reports_503_and_a_reason_when_no_radio(tmp_path):
    # dump1090 present (injected which) so this pins the no-radio refusal, not a missing tool.
    with _client(tmp_path,
                 adsb_factory=lambda: AdsbDecoder(radio_present=lambda: False,
                                                  which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/adsb")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["aircraft"] == []          # never a fabricated contact
    assert "RTL-SDR" in body["reason"]


def test_adsb_default_decoder_also_refuses_without_a_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/adsb")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_adsb_returns_the_located_aircraft_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 adsb_factory=lambda: _StubAdsb(_aircraft())) as client:
        resp = client.get("/api/adsb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_hz"] == 1_090_000_000
    assert body["receiver_located"] is True
    plane = body["aircraft"][0]
    assert plane["callsign"] == "UAL123"
    assert plane["altitude_ft"] == 37000
    assert plane["range_nm"] == 12.4
    assert plane["bearing_deg"] == 41.0


def test_adsb_empty_sky_is_honest_not_a_fabricated_plane(tmp_path):
    with _client(tmp_path, probe=_present,
                 adsb_factory=lambda: _StubAdsb([])) as client:
        resp = client.get("/api/adsb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["aircraft"] == []


def test_adsb_stands_the_sweeper_down(tmp_path):
    released = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def release(self):
            released["value"] = True
            self.stop()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 adsb_factory=lambda: _StubAdsb(_aircraft())) as client:
        client.get("/api/adsb")
    assert released["value"] is True


# --- aprs (APRS roster, M14) --------------------------------------------------


def test_aprs_reports_503_and_a_reason_when_no_radio(tmp_path):
    with _client(tmp_path,
                 aprs_factory=lambda: AprsDecoder(radio_present=lambda: False,
                                                  which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/aprs")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["stations"] == []          # never a fabricated station
    assert "RTL-SDR" in body["reason"]


def test_aprs_default_decoder_also_refuses_without_a_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/aprs")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_aprs_returns_the_roster_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 aprs_factory=lambda: _StubAprs(_aprs_stations())) as client:
        resp = client.get("/api/aprs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_hz"] == 144_390_000
    s = body["stations"][0]
    assert s["source"] == "KD2ABC-9"
    assert s["kind"] == "position"
    assert s["lat"] == 40.70


def test_aprs_stands_the_sweeper_down(tmp_path):
    released = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def release(self):
            released["value"] = True
            self.stop()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 aprs_factory=lambda: _StubAprs(_aprs_stations())) as client:
        client.get("/api/aprs")
    assert released["value"] is True


# --- pocsag (POCSAG page log, M15) --------------------------------------------


def test_pocsag_reports_503_and_a_reason_when_no_radio(tmp_path):
    with _client(tmp_path,
                 pocsag_factory=lambda: PocsagDecoder(
                     radio_present=lambda: False,
                     which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/pocsag")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["pages"] == []             # never a fabricated page
    assert "RTL-SDR" in body["reason"]


def test_pocsag_default_decoder_also_refuses_without_a_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/pocsag")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_pocsag_returns_the_pages_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 pocsag_factory=lambda: _StubPocsag(_pocsag_pages())) as client:
        resp = client.get("/api/pocsag")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_hz"] == 929_612_500
    page = body["pages"][0]
    assert page["address"] == 1234567
    assert page["kind"] == "alpha"
    assert page["text"] is None
    assert page["length"] == 20


def test_pocsag_never_serves_the_words_of_a_commercial_page(tmp_path):
    # End to end through the real decoder, faked only at the rtl_fm|multimon-ng boundary: the
    # framing reaches the API and the words never do — not in the body, not in the event log.
    line = "POCSAG1200: Address: 1234567  Function: 3  Alpha:   CALL EXT 2210 URGENT"

    class _Pipeline:
        stdout = iter([line])

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    decoder = PocsagDecoder(spawn=lambda f, d: _Pipeline(), radio_present=lambda: True,
                            which=lambda name: "/usr/bin/" + name)
    with _client(tmp_path, probe=_present, pocsag_factory=lambda: decoder) as client:
        client.get("/api/pocsag")
        decoder.drain()
        resp = client.get("/api/pocsag")
    assert resp.status_code == 200
    page = resp.json()["pages"][0]
    assert (page["address"], page["kind"], page["baud"], page["length"]) == (1234567, "alpha",
                                                                              1200, 20)
    assert page["text_withheld"] is True
    assert "CALL EXT" not in resp.text
    assert "CALL EXT" not in (tmp_path / "events.jsonl").read_text()


def test_pocsag_stands_the_sweeper_down(tmp_path):
    released = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def release(self):
            released["value"] = True
            self.stop()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 pocsag_factory=lambda: _StubPocsag(_pocsag_pages())) as client:
        client.get("/api/pocsag")
    assert released["value"] is True


# --- acars (ACARS message log, M16) -------------------------------------------


def test_acars_reports_503_and_a_reason_when_no_radio(tmp_path):
    with _client(tmp_path,
                 acars_factory=lambda: AcarsDecoder(
                     radio_present=lambda: False,
                     which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/acars")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["messages"] == []          # never a fabricated message
    assert "RTL-SDR" in body["reason"]


def test_acars_default_decoder_also_refuses_without_a_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/acars")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_acars_returns_the_messages_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 acars_factory=lambda: _StubAcars(_acars_messages())) as client:
        resp = client.get("/api/acars")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_hz"] == 131_550_000
    m = body["messages"][0]
    assert m["tail"] == "F-GTAE"
    assert m["flight"] == "AF7728"
    assert m["label"] == "H1"


def test_acars_stands_the_sweeper_down(tmp_path):
    released = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def release(self):
            released["value"] = True
            self.stop()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 acars_factory=lambda: _StubAcars(_acars_messages())) as client:
        client.get("/api/acars")
    assert released["value"] is True


def test_spectrum_releases_the_acars_decoder(tmp_path):
    """Switching back to the waterfall releases the ACARS decoder — one program per dongle."""
    released = {"value": False}

    class _RecordingAcars(_StubAcars):
        def release(self):
            released["value"] = True
            super().release()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows()),
                 acars_factory=lambda: _RecordingAcars(_acars_messages())) as client:
        client.get("/api/acars")         # acarsdec takes the dongle
        client.get("/api/spectrum")      # sweep reclaims it
    assert released["value"] is True


# --- ais (AIS vessel roster, M26) ---------------------------------------------


def test_ais_reports_503_and_a_reason_when_no_radio(tmp_path):
    # A compatible companion makes this pin the no-radio refusal.
    companion = tmp_path / "rtl_ais-sigint101"
    companion.write_text("#!/bin/sh\necho sigint101-ais-udp-v1\n", encoding="utf-8")
    companion.chmod(0o755)
    with _client(tmp_path,
                 ais_factory=lambda: AisDecoder(
                     radio_present=lambda: False, port=0,
                     which=lambda _: str(companion))) as client:
        resp = client.get("/api/ais")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["vessels"] == []           # never a fabricated vessel
    assert "RTL-SDR" in body["reason"]


def test_ais_default_decoder_also_refuses_without_a_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/ais")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_ais_returns_the_vessels_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 ais_factory=lambda: _StubAis(_ais_vessels())) as client:
        resp = client.get("/api/ais")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["channel_a_hz"] == 161_975_000
    assert body["channel_b_hz"] == 162_025_000
    v = body["vessels"][0]
    assert v["mmsi"] == 351759000
    assert v["name"] == "EVER DIADEM"
    assert v["nav_status"] == "Moored"


# --- dmr (DMR/P25 call log, M27) ----------------------------------------------


def test_dmr_reports_503_and_a_reason_when_no_radio(tmp_path):
    with _client(tmp_path,
                 dmr_factory=lambda: DmrDecoder(
                     radio_present=lambda: False,
                     which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/dmr")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["calls"] == []             # never a fabricated call
    assert "RTL-SDR" in body["reason"]


def test_dmr_default_decoder_also_refuses_without_a_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/dmr")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_dmr_returns_the_calls_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 dmr_factory=lambda: _StubDmr(_dmr_calls())) as client:
        resp = client.get("/api/dmr")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_hz"] == 450_000_000
    c = body["calls"][0]
    assert c["protocol"] == "P25"
    assert c["talkgroup"] == 2001
    assert c["source"] == 1234567
    assert c["kind"] == "group"


# --- apt (NOAA APT weather image, M24) ----------------------------------------


def test_apt_reports_503_and_a_reason_when_no_radio(tmp_path):
    with _client(tmp_path,
                 apt_factory=lambda: AptDecoder(
                     output_dir=tmp_path / "apt",
                     radio_present=lambda: False,
                     which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/apt")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["images"] == []            # never a placeholder image
    assert "RTL-SDR" in body["reason"]


def test_apt_default_decoder_also_refuses_without_a_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/apt")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_apt_returns_the_images_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 apt_factory=lambda: _StubApt(_apt_images())) as client:
        resp = client.get("/api/apt")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_hz"] == 137_100_000
    img = body["images"][0]
    assert img["satellite"] == "NOAA-19"
    assert img["path"] == "apt/NOAA-19/avhrr.png"


# --- sstv (SSTV picture, M25) --------------------------------------------------


def test_sstv_reports_503_and_a_reason_when_no_radio(tmp_path):
    with _client(tmp_path,
                 sstv_factory=lambda: SstvDecoder(
                     output_dir=tmp_path / "sstv",
                     radio_present=lambda: False,
                     which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/sstv")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["images"] == []            # never a placeholder image
    assert "RTL-SDR" in body["reason"]


def test_sstv_default_decoder_also_refuses_without_a_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/sstv")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_sstv_returns_the_images_when_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 sstv_factory=lambda: _StubSstv(_sstv_images())) as client:
        resp = client.get("/api/sstv")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    img = body["images"][0]
    assert img["mode"] == "Martin 1"
    assert img["path"] == "sstv/20261030T120000-000.png"


# --- /media (serves only the image decoders' PNGs, nothing else under data/) ---


def test_media_serves_a_decoded_png_under_apt(tmp_path):
    png = tmp_path / "apt" / "shot.png"
    png.parent.mkdir(parents=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\n fake png bytes")
    with _client(tmp_path) as client:
        resp = client.get("/media/apt/shot.png")
    assert resp.status_code == 200
    assert resp.content.startswith(b"\x89PNG")


def test_media_refuses_a_path_outside_the_image_folders(tmp_path):
    # A PNG that sits under data/ but NOT in apt/sstv must not be served (no DB/events leak).
    loose = tmp_path / "loose.png"
    loose.write_bytes(b"\x89PNG not in an image folder")
    with _client(tmp_path) as client:
        assert client.get("/media/loose.png").status_code == 404
        # Path traversal back out of the image folders is refused too.
        assert client.get("/media/apt/../loose.png").status_code == 404


def test_media_refuses_a_non_png(tmp_path):
    txt = tmp_path / "sstv" / "notes.txt"
    txt.parent.mkdir(parents=True)
    txt.write_text("nope")
    with _client(tmp_path) as client:
        assert client.get("/media/sstv/notes.txt").status_code == 404


def test_flock_reports_503_and_a_reason_when_no_monitor_radio(tmp_path):
    with _client(tmp_path,
                 flock_factory=lambda: FlockDetector(
                     radio_present=lambda: False,
                     which=lambda name: "/usr/bin/" + name)) as client:
        resp = client.get("/api/flock")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["detections"] == []          # never a fabricated detection
    assert "monitor interface" in body["reason"]


def test_flock_default_detector_also_refuses_without_the_radio(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/flock")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_flock_returns_detections_when_running(tmp_path):
    with _client(tmp_path, flock_factory=lambda: _StubFlock(_flock_detections())) as client:
        resp = client.get("/api/flock")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["interface"] == "wlan1"
    d = body["detections"][0]
    assert d["mac"] == "b4:1e:52:00:00:01"
    assert d["tier"] == 4


def test_flock_is_a_separate_radio_and_leaves_the_sweeper_alone(tmp_path):
    """The Flock detector reads wlan1, not the SDR, so /api/flock never stands the sweeper down."""
    released = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def release(self):
            released["value"] = True
            self.stop()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 flock_factory=lambda: _StubFlock(_flock_detections())) as client:
        client.get("/api/flock")
    assert released["value"] is False


# --- rayhunter (Orbic cell-site-simulator detector, M19) ----------------------


def _unreachable_fetch(url, timeout):
    raise OSError("connection refused")


def test_rayhunter_reports_503_and_a_reason_when_the_orbic_is_unreachable(tmp_path):
    with _client(tmp_path,
                 rayhunter_factory=lambda: RayhunterMonitor(fetch=_unreachable_fetch)) as client:
        resp = client.get("/api/rayhunter")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["warnings"] == []          # never a fabricated finding
    assert body["status"] == "off"
    assert "Rayhunter" in body["reason"] or "reachable" in body["reason"]


class _RouterLoginPage:
    """What urlopen hands back when a home router's login page answers at the Orbic's address."""

    status = 200

    def read(self):
        return b"<!DOCTYPE html><html><head><title>Router login</title></head></html>"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _no_route(url, timeout):
    raise urllib.error.URLError("no route to host")


def _not_found(url, timeout):
    raise urllib.error.HTTPError(url, 404, "Not Found", None, io.BytesIO(b"Not Found"))


def _router_login(url, timeout):
    return _RouterLoginPage()


@pytest.mark.parametrize("urlopen", [_no_route, _not_found, _router_login],
                         ids=["nothing-answers", "http-404", "a-router-answers"])
def test_rayhunter_default_monitor_also_refuses_without_an_orbic(tmp_path, monkeypatch, urlopen):
    # The default monitor's own fetch, with the answer injected where it meets the network:
    # nothing leaves the machine, and whatever answers at 192.168.1.1:8080 here cannot change it.
    seen = []

    def answer(url, timeout):
        seen.append(url)
        return urlopen(url, timeout)

    monkeypatch.setattr(urllib.request, "urlopen", answer)
    with _client(tmp_path) as client:
        resp = client.get("/api/rayhunter")
    assert seen and all(url.startswith("http://192.168.1.1:8080/") for url in seen)
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def _answers(manifest, report=(404, b"Analysis file not found")):
    """A fake HTTP GET at the boundary: each manifest and report request answered as given."""

    def fetch(url, timeout):
        if url.endswith("/api/qmdl-manifest"):
            return manifest
        return report

    return fetch


# Rayhunter's manifest while it records: entries, and the active recording as current_entry.
_RH_RECORDING = (200, b'{"entries": [], "current_entry": {"name": "1730300000"}}')


@pytest.mark.parametrize("manifest", [
    (500, b"Internal Server Error"),
    (404, b"Not Found"),
    (200, b"<!DOCTYPE html><html><head><title>Router login</title></head></html>"),
    (200, b'{"entries": []}'),
    (200, b'{"current_entry": null}'),
], ids=["http-500", "http-404", "html-200", "no-current_entry", "no-entries"])
def test_rayhunter_refuses_an_answer_that_is_not_a_rayhunter_manifest(tmp_path, manifest):
    # An error page, or a router or captive portal at the Orbic's address, is not "LIVE, not
    # recording": it is the same 503 with a reason as an Orbic that is not there.
    with _client(tmp_path,
                 rayhunter_factory=lambda: RayhunterMonitor(fetch=_answers(manifest))) as client:
        resp = client.get("/api/rayhunter")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["status"] == "off"
    assert body["warnings"] == []
    assert "Rayhunter" in body["reason"]


@pytest.mark.parametrize("code", [500, 503])
def test_rayhunter_refuses_when_the_recordings_report_cannot_be_read(tmp_path, code):
    # A report the console could not read is never "recording · clean".
    fetch = _answers(_RH_RECORDING, report=(code, b"Internal Server Error"))
    with _client(tmp_path, rayhunter_factory=lambda: RayhunterMonitor(fetch=fetch)) as client:
        resp = client.get("/api/rayhunter")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["status"] != "clean"
    assert str(code) in body["reason"]


def test_rayhunter_says_no_report_yet_not_clean_before_the_report_exists(tmp_path):
    # Recording, but with no analysis report (404) nothing has been read to call clean.
    fetch = _answers(_RH_RECORDING, report=(404, b"Analysis file not found"))
    with _client(tmp_path, rayhunter_factory=lambda: RayhunterMonitor(fetch=fetch)) as client:
        resp = client.get("/api/rayhunter")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["status"] == "no-report"
    assert body["warnings"] == []


# Lesson 19's panel shows the reason on one line of at most 66 characters at 800x480 (18 px PT Mono
# in a 738 px column); a second line falls below the panel's fixed height and is cut off.
_REASON_FITS = 66


def _long_unreachable_fetch(url, timeout):
    raise OSError("the remote end closed the connection without a response after a long wait")


@pytest.mark.parametrize("fetch,facts", [
    (_unreachable_fetch, ["reachable", "192.168.1.1:8080"]),
    (_long_unreachable_fetch, ["reachable", "192.168.1.1:8080"]),
    (_answers((500, b"Internal Server Error")), ["500", "not rayhunter", "192.168.1.1:8080"]),
    (_answers((200, b"<html><head><title>Router login</title></head></html>")),
     ["not rayhunter", "192.168.1.1:8080"]),
    (_answers(_RH_RECORDING, report=(503, b"busy")), ["503", "report"]),
    (_answers(_RH_RECORDING, report=(200, b"")), ["report", "read"]),
], ids=["no-answer", "no-answer-long-cause", "http-error", "not-rayhunter", "report-http-error",
        "report-not-a-report"])
def test_rayhunter_refusal_says_what_happened_in_one_panel_line(tmp_path, fetch, facts):
    # The panel keeps its "orbic unreachable" headline and shows this reason under it, so the
    # reason tells no answer, an HTTP error, another site's page and an unreadable report apart,
    # in a line the panel shows whole.
    with _client(tmp_path, rayhunter_factory=lambda: RayhunterMonitor(fetch=fetch)) as client:
        resp = client.get("/api/rayhunter")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["status"] == "off"
    reason = body["reason"]
    assert all(fact in reason.lower() for fact in facts), reason
    assert len(reason) <= _REASON_FITS, (len(reason), reason)


def test_rayhunter_returns_the_line_and_findings_when_reachable(tmp_path):
    warns = _rayhunter_warnings()
    with _client(tmp_path,
                 rayhunter_factory=lambda: _StubRayhunter("warning", warns)) as client:
        resp = client.get("/api/rayhunter")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["status"] == "warning"
    assert body["host"] == "192.168.1.1"
    w = body["warnings"][0]
    assert w["name"] == "IMSI Requested"
    assert w["severity"] == "low"


def test_rayhunter_is_a_separate_device_and_leaves_the_sweeper_alone(tmp_path):
    """The Orbic is read over WiFi, not the SDR, so /api/rayhunter never stands the sweeper down."""
    released = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def release(self):
            released["value"] = True
            self.stop()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 rayhunter_factory=lambda: _StubRayhunter("clean", [])) as client:
        client.get("/api/rayhunter")
    assert released["value"] is False


def test_flipper_reports_the_device_when_attached(tmp_path):
    with _client(tmp_path,
                 flipper_factory=lambda: _StubFlipper(device=_flipper_device())) as client:
        resp = client.get("/api/flipper")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["device"]["firmware"] == "1.4.3"
    assert body["device"]["name"] == "Ex4mple"


def test_flipper_reports_503_and_a_reason_when_absent(tmp_path):
    from sigint101.flipper import FlipperError
    with _client(tmp_path,
                 flipper_factory=lambda: _StubFlipper(
                     error=FlipperError("no Flipper attached"))) as client:
        resp = client.get("/api/flipper")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["device"] is None          # never a fabricated device line
    assert "no Flipper" in body["reason"]
    assert body["command"] is None         # read over its CLI, not run as a tool: no command line


def _flipper_blocked_count(tmp_path):
    from sigint101.events import read_events
    return [e["kind"] for e in read_events(tmp_path / "events.jsonl")].count("flipper_blocked")


def test_a_missing_flipper_is_logged_once_not_every_poll(tmp_path):
    # The panel polls every few seconds, and a console with no Flipper plugged in is the usual
    # case: one flipper_blocked line for the episode, never one per poll (the T-15 log growth).
    from sigint101.flipper import FlipperError
    with _client(tmp_path, flipper_factory=lambda: _StubFlipper(
            error=FlipperError("no Flipper on USB"))) as client:
        for _ in range(10):
            assert client.get("/api/flipper").status_code == 503
    assert _flipper_blocked_count(tmp_path) == 1


def test_a_flipper_refusal_is_logged_again_after_a_good_read(tmp_path):
    # The dedupe is per episode: once the device has answered, losing it again is news.
    from sigint101.flipper import FlipperError
    stub = _StubFlipper(error=FlipperError("no Flipper on USB"))
    with _client(tmp_path, flipper_factory=lambda: stub) as client:
        client.get("/api/flipper")
        stub._error, stub._device = None, _flipper_device()
        live = client.get("/api/flipper").json()
        stub._error = FlipperError("no Flipper on USB")
        client.get("/api/flipper")
    assert live["available"] is True and live["command"] is None
    assert _flipper_blocked_count(tmp_path) == 2


def test_flipper_is_a_separate_device_and_leaves_the_sweeper_alone(tmp_path):
    """The Flipper is read over USB serial, not the SDR, so /api/flipper leaves the sweep alone."""
    released = {"value": False}

    class _RecordingSweeper(_StubSweeper):
        def release(self):
            released["value"] = True
            self.stop()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _RecordingSweeper(_rows()),
                 flipper_factory=lambda: _StubFlipper(device=_flipper_device())) as client:
        client.get("/api/flipper")
    assert released["value"] is False


def test_spectrum_releases_the_pocsag_decoder(tmp_path):
    """Switching back to the waterfall releases the POCSAG decoder — one program per dongle."""
    released = {"value": False}

    class _RecordingPocsag(_StubPocsag):
        def release(self):
            released["value"] = True
            super().release()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows()),
                 pocsag_factory=lambda: _RecordingPocsag(_pocsag_pages())) as client:
        client.get("/api/pocsag")        # multimon-ng takes the dongle
        client.get("/api/spectrum")      # sweep reclaims it
    assert released["value"] is True


def test_spectrum_releases_the_aprs_decoder(tmp_path):
    """Switching back to the waterfall releases the APRS decoder — one program per dongle."""
    released = {"value": False}

    class _RecordingAprs(_StubAprs):
        def release(self):
            released["value"] = True
            super().release()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows()),
                 aprs_factory=lambda: _RecordingAprs(_aprs_stations())) as client:
        client.get("/api/aprs")          # multimon-ng takes the dongle
        client.get("/api/spectrum")      # sweep reclaims it
    assert released["value"] is True


def test_spectrum_releases_the_adsb_decoder(tmp_path):
    """Switching back to the waterfall releases the ADS-B decoder — one program per dongle."""
    released = {"value": False}

    class _RecordingAdsb(_StubAdsb):
        def release(self):
            released["value"] = True
            super().release()

    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows()),
                 adsb_factory=lambda: _RecordingAdsb(_aircraft())) as client:
        client.get("/api/adsb")          # dump1090 takes the dongle
        client.get("/api/spectrum")      # sweep reclaims it
    assert released["value"] is True


# --- hardware (probe reported as data) ----------------------------------------


def test_hardware_reports_a_present_radio(tmp_path):
    with _client(tmp_path, probe=_present) as client:
        resp = client.get("/api/hardware")
    assert resp.status_code == 200
    body = resp.json()
    assert body["present"] is True
    assert body["method"] == "lsusb"
    assert "RTL2832U" in body["detail"]


def test_hardware_reports_an_absent_radio(tmp_path):
    with _client(tmp_path, probe=_absent) as client:
        resp = client.get("/api/hardware")
    assert resp.json()["present"] is False


def test_hardware_bar_follows_the_probe_and_real_sdr_ownership(tmp_path):
    probe_reply = _present()

    def probe():
        return probe_reply

    with _client(tmp_path, probe=probe, sweeper_factory=lambda: _StubSweeper([])) as client:
        def radio():
            return client.get("/api/hardware").json()["state"]

        assert radio() == "present"
        with client.app.state.radio_arbiter.reserve("ppm", AssertionError, float("inf")):
            assert radio() == "busy"
        assert radio() == "present"
        assert client.get("/api/spectrum").status_code == 200
        assert radio() == "busy"
        probe_reply = _absent()
        assert radio() == "absent"
        probe_reply = RadioProbe(False, "lsusb", "probe failed", known=False)
        assert radio() == "unknown"
        probe_reply = _present()
        client.app.state.sweeper.release()
        assert radio() == "present"


def test_status_bar_uses_last_measured_ppm(tmp_path):
    def measure(**_kwargs):
        return {"ppm": -12.5, "reason": ""}

    with _client(tmp_path, probe=_present, ppm_measure=measure) as client:
        assert client.get("/api/hardware").json()["ppm"] is None
        assert client.post("/api/ppm/measure").json()["ppm"] == -12.5
        assert client.get("/api/hardware").json()["ppm"] == -12.5
        assert "PPM -12.5</span>" in client.get("/").text


# --- per-lesson tune band (the instrument starts on the lesson's band) --------


def test_a_tuned_lesson_renders_its_own_band(tmp_path):
    """Fixture lesson 2 declares a 433.92 MHz band, so its console points there, not at FM.

    The dial reads the band centre, the step is the pack's, and the live waterfall carries the
    band edges the console retunes the sweep to.
    """
    with _client(tmp_path) as client:
        client.post("/api/predict?lesson_id=2", data={"prediction": "a"},
                    headers={"content-type": "application/x-www-form-urlencoded"})
        resp = client.get("/lesson/2")
    assert resp.status_code == 200
    assert "433.920" in resp.text
    assert 'data-tune-low="432920000"' in resp.text
    assert 'data-tune-high="434920000"' in resp.text


def test_an_untuned_lesson_keeps_the_default_fm_band(tmp_path):
    """Lesson 1 declares no band, so its console keeps the FM default and emits no retune edges."""
    with _client(tmp_path) as client:
        resp = client.get("/lesson/1")
    assert resp.status_code == 200
    assert "data-tune-low" not in resp.text


# --- tune (fail-closed with the reason) ---------------------------------------


def test_tune_refuses_an_out_of_range_span_with_the_reason(tmp_path):
    with _client(tmp_path, probe=_present) as client:
        resp = client.post("/api/tune", json={"low": 10_000_000, "high": 20_000_000})
    assert resp.status_code == 400
    assert resp.json()["tuned"] is False
    assert "tunes" in resp.json()["reason"]  # "this radio tunes 24-1766 MHz ..."


def test_tune_refuses_a_span_that_asks_for_more_bins_than_a_row_holds(tmp_path):
    # 2 MHz at 100 Hz asks rtl_power for 20,000 bins every row; refused before any radio is touched.
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.post("/api/tune",
                           json={"low": 95_000_000, "high": 97_000_000, "bin_size": 100})
    assert resp.status_code == 400
    assert "bins" in resp.json()["reason"]


def test_tune_reports_the_bin_size_it_applied(tmp_path):
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.post("/api/tune", json={"low": 1_089_000_000, "high": 1_091_000_000,
                                              "bin_size": 31_250})
        spectrum = client.get("/api/spectrum").json()
    assert resp.json()["bin_size"] == 31_250
    assert (spectrum["freq_low"], spectrum["freq_high"], spectrum["bin_size"]) == (
        1_089_000_000, 1_091_000_000, 31_250)


def test_tune_without_a_radio_is_503(tmp_path):
    with _client(tmp_path, probe=_absent) as client:
        resp = client.post("/api/tune", json={"low": 100_000_000, "high": 104_000_000})
    assert resp.status_code == 503
    assert resp.json()["tuned"] is False


def test_tune_accepts_a_valid_span_when_a_radio_is_present(tmp_path):
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.post("/api/tune", json={"low": 100_000_000, "high": 104_000_000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tuned"] is True
    assert body["low"] == 100_000_000
    assert body["high"] == 104_000_000


# --- gain (M2: "step the gain from the bottom up") ------------------------------------


class _IdleProc:
    """A launched rtl_power at the process boundary: alive, printing nothing yet."""

    def __init__(self):
        self.stdout = iter(())

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def _gain_client(tmp_path, spawned):
    """A console whose real Sweeper launches recorded rtl_power command lines."""
    def spawn(cmd):
        spawned.append(list(cmd))
        return _IdleProc()
    return _client(tmp_path, probe=_present,
                   sweeper_factory=lambda: Sweeper(spawn=spawn, radio_present=lambda: True))


def test_a_gain_step_restarts_the_sweep_with_that_gain(tmp_path):
    spawned = []
    with _gain_client(tmp_path, spawned) as client:
        client.get("/api/spectrum")
        resp = client.post("/api/gain", json={"gain": 29.7})
        spectrum = client.get("/api/spectrum").json()
    assert resp.status_code == 200
    assert resp.json() == {"set": True, "gain": 29.7}
    # The new gain reaches the rtl_power actually launched, not just the reply.
    assert spawned[-1][spawned[-1].index("-g") + 1] == "29.7"
    assert spectrum["gain"] == 29.7


def test_a_null_gain_hands_the_tuner_back_to_automatic(tmp_path):
    spawned = []
    with _gain_client(tmp_path, spawned) as client:
        client.post("/api/gain", json={"gain": 12.5})
        client.get("/api/spectrum")
        resp = client.post("/api/gain", json={"gain": None})
        spectrum = client.get("/api/spectrum").json()
    assert resp.json() == {"set": True, "gain": None}
    assert "-g" not in spawned[-1]
    assert spectrum["gain"] is None


@pytest.mark.parametrize("bad", [60, -1, "loud"])
def test_a_gain_the_tuner_cannot_do_is_refused_with_the_reason(tmp_path, bad):
    spawned = []
    with _gain_client(tmp_path, spawned) as client:
        resp = client.post("/api/gain", json={"gain": bad})
    assert resp.status_code == 400
    assert resp.json()["set"] is False
    assert "gain" in resp.json()["reason"]


def test_gain_without_a_radio_is_503_and_changes_nothing(tmp_path):
    with _client(tmp_path, probe=_absent) as client:
        resp = client.post("/api/gain", json={"gain": 29.7})
        spectrum = client.get("/api/spectrum")
    assert resp.status_code == 503
    assert resp.json()["set"] is False
    assert spectrum.status_code == 503


# --- exam trainer: one study card, commit-before-reveal (like the predict gate) ---------

# A known-good pool question to drive the answer/review endpoints with, independent of which card
# /exam happens to surface first (that ordering is the store's business, not these tests').
_Q = exam.load_pool().get("T1A01")


def test_exam_serves_a_study_card_with_tap_choices_and_no_answer(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/exam")
    assert resp.status_code == 200
    # It surfaces a real pool question (its id is in the eyebrow) with four tap choices.
    served = re.search(r"EXAM TRAINER &middot; (T\w+)", resp.text).group(1)
    assert exam.load_pool().get(served) is not None
    assert resp.text.count('name="choice"') == 4
    # Commit-before-reveal: the correct answer is not marked until the learner has answered.
    assert "exam-reveal" not in resp.text


def test_exam_answering_records_and_reveals_the_result(tmp_path):
    with _client(tmp_path) as client:
        posted = client.post("/api/exam/answer?question_id=T1A01",
                             data={"choice": _Q.answer},
                             headers={"content-type": "application/x-www-form-urlencoded"},
                             follow_redirects=False)
        # The gate redirects to the review of the card just answered, with no JavaScript.
        assert posted.status_code == 303
        review = client.get(posted.headers["location"])
    assert review.status_code == 200
    assert "exam-reveal" in review.text
    # The correct option is marked, and a right answer reads as correct.
    assert "exam-chip--correct" in review.text
    assert "Correct" in review.text


def test_exam_a_wrong_answer_still_reveals_the_right_one(tmp_path):
    wrong = next(letter for letter in "ABCD" if letter != _Q.answer)
    with _client(tmp_path) as client:
        posted = client.post("/api/exam/answer?question_id=T1A01",
                             data={"choice": wrong},
                             headers={"content-type": "application/x-www-form-urlencoded"},
                             follow_redirects=False)
        review = client.get(posted.headers["location"])
    assert review.status_code == 200
    assert "exam-chip--correct" in review.text   # the right answer is always shown
    assert "exam-chip--chosen" in review.text     # and the learner's miss is marked


def test_exam_answer_for_an_unknown_question_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/exam/answer?question_id=NOPE99",
                           data={"choice": "A"},
                           headers={"content-type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 404


# --- exam trainer: scored 35-question practice sim (answer-then-advance) -----------------

def _pool_answer(qid):
    return exam.load_pool().get(qid).answer


def _drive_sim(client, *, correct_count=35):
    """Answer the whole sim via the real flow: the first correct_count right, the rest wrong.

    Returns the location /exam/sim redirects to once every question is answered.
    """
    pool = exam.load_pool()
    answered = 0
    for _ in range(80):
        page = client.get("/exam/sim", follow_redirects=False)
        if page.status_code in (302, 303, 307):
            return page.headers["location"]
        qid = re.search(r"question_id=(T\w+)", page.text).group(1)
        q = pool.get(qid)
        choice = q.answer if answered < correct_count else next(c for c in "ABCD" if c != q.answer)
        answered += 1
        client.post(f"/api/exam/sim/answer?question_id={qid}",
                    data={"choice": choice},
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    follow_redirects=False)
    raise AssertionError("sim did not finish in 80 steps")


def test_exam_sim_starts_and_serves_the_first_question(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/exam/sim")
    assert page.status_code == 200
    assert "Question 1 of 35" in page.text
    # A real pool question with four tap choices, posting to the sim endpoint, no reveal yet.
    assert page.text.count('name="choice"') == 4
    assert "/api/exam/sim/answer" in page.text
    assert "exam-reveal" not in page.text


def test_exam_sim_answer_records_and_redirects_to_its_review(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/exam/sim")
        qid = re.search(r"question_id=(T\w+)", page.text).group(1)
        posted = client.post(f"/api/exam/sim/answer?question_id={qid}",
                             data={"choice": _pool_answer(qid)},
                             headers={"content-type": "application/x-www-form-urlencoded"},
                             follow_redirects=False)
        assert posted.status_code == 303
        assert f"/exam/sim/review/{qid}" in posted.headers["location"]
        review = client.get(posted.headers["location"])
    assert review.status_code == 200
    assert "exam-chip--correct" in review.text        # the answer is revealed
    assert "Question 1 of 35" in review.text           # progress stays visible


def test_exam_sim_runs_to_a_passing_results_screen(tmp_path):
    with _client(tmp_path) as client:
        where = _drive_sim(client, correct_count=35)
        assert "/exam/sim/results" in where
        results = client.get("/exam/sim/results")
    assert results.status_code == 200
    assert "35 of 35" in results.text
    assert "exam-results--pass" in results.text
    assert "26" in results.text                         # the 74% pass line is shown


def test_exam_sim_below_the_pass_mark_reads_as_not_passed(tmp_path):
    with _client(tmp_path) as client:
        _drive_sim(client, correct_count=25)            # one below 26/35
        results = client.get("/exam/sim/results")
    assert results.status_code == 200
    assert "25 of 35" in results.text
    assert "exam-results--fail" in results.text


def test_exam_sim_results_before_finishing_resumes_the_sim(tmp_path):
    with _client(tmp_path) as client:
        client.get("/exam/sim")                          # starts a sim, nothing answered
        resp = client.get("/exam/sim/results", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    assert resp.headers["location"].endswith("/exam/sim")


def test_exam_sim_restart_clears_and_starts_fresh(tmp_path):
    with _client(tmp_path) as client:
        _drive_sim(client, correct_count=35)
        restarted = client.post("/api/exam/sim/restart", follow_redirects=False)
        assert restarted.status_code == 303
        page = client.get(restarted.headers["location"])
    assert "Question 1 of 35" in page.text              # a brand-new sim, back at the start


def test_exam_sim_answer_for_an_unknown_question_is_404(tmp_path):
    with _client(tmp_path) as client:
        client.get("/exam/sim")
        resp = client.post("/api/exam/sim/answer?question_id=NOPE99",
                           data={"choice": "A"},
                           headers={"content-type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 404


_FORM = {"content-type": "application/x-www-form-urlencoded"}


def _sim_current(client):
    """The question id /exam/sim is serving now, and the page."""
    page = client.get("/exam/sim", follow_redirects=False)
    return re.search(r"sim/answer\?question_id=(T\w+)", page.text).group(1), page


def _sim_answer(client, qid, choice):
    return client.post(f"/api/exam/sim/answer?question_id={qid}", data={"choice": choice},
                       headers=_FORM, follow_redirects=False)


def _wrong(qid):
    return next(c for c in "ABCD" if c != _pool_answer(qid))


def test_exam_sim_answer_cannot_change_after_review(tmp_path):
    # T-10: answer wrong, see the reveal, then POST the right letter (history/resubmit or a direct
    # POST). The stored answer, the running score and the Leitner credit must all stay put.
    with _client(tmp_path) as client:
        qid, _ = _sim_current(client)
        _sim_answer(client, qid, _wrong(qid))
        again = _sim_answer(client, qid, _pool_answer(qid))
        assert again.status_code == 303
        assert again.headers["location"].endswith(f"/exam/sim/review/{qid}")
        review = client.get(again.headers["location"])
        store = client.app.state.exam_store
        assert store.sim()["answers"] == {qid: _wrong(qid)}
        state = store.state(qid)
    assert state["seen"] == 1 and state["correct"] == 0      # not re-credited
    assert "Not quite" in review.text
    assert "0 right of 1 answered" in review.text


def test_exam_sim_answer_that_is_not_an_option_is_not_recorded(tmp_path):
    # Answers are final, so a blank or stray letter (a direct POST; the card's radios are
    # required) must not lock the question in as wrong. It goes back to the question instead.
    with _client(tmp_path) as client:
        qid, _ = _sim_current(client)
        for bad in ("", "Z"):
            resp = _sim_answer(client, qid, bad)
            assert resp.status_code == 303
            assert resp.headers["location"].endswith("/exam/sim")
        sim = client.app.state.exam_store.sim()
        state = client.app.state.exam_store.state(qid)
    assert sim["answers"] == {}
    assert state is None


def test_exam_sim_review_shows_the_stored_answer_not_the_query(tmp_path):
    # The review reads the learner's choice from the sim, so a hand-edited ?choice= can't
    # turn a miss into "Correct".
    with _client(tmp_path) as client:
        qid, _ = _sim_current(client)
        _sim_answer(client, qid, _wrong(qid))
        review = client.get(f"/exam/sim/review/{qid}?choice={_pool_answer(qid)}")
    assert "Not quite" in review.text
    assert "exam-chip--chosen" in review.text


def test_exam_sim_review_refuses_an_unanswered_question(tmp_path):
    # The reveal is only for a question already answered; asking for one early goes back to it.
    with _client(tmp_path) as client:
        qid, _ = _sim_current(client)
        resp = client.get(f"/exam/sim/review/{qid}", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("/exam/sim")
        page = client.get(resp.headers["location"])
    assert "exam-chip--correct" not in page.text
    assert "exam-reveal" not in page.text


def test_exam_sim_card_offers_skip(tmp_path):
    with _client(tmp_path) as client:
        qid, page = _sim_current(client)
    assert f"/api/exam/sim/skip?question_id={qid}" in page.text
    assert ">Skip<" in page.text


def test_exam_sim_skip_serves_the_next_question_and_keeps_the_skipped_one(tmp_path):
    with _client(tmp_path) as client:
        first, page = _sim_current(client)
        assert "Question 1 of 35" in page.text
        skipped = client.post(f"/api/exam/sim/skip?question_id={first}", follow_redirects=False)
        assert skipped.status_code == 303
        assert skipped.headers["location"].endswith("/exam/sim")
        second, page = _sim_current(client)
        sim = client.app.state.exam_store.sim()
    assert second != first
    assert "Question 2 of 35" in page.text
    assert "1 skipped" in page.text
    assert first not in sim["answers"]                   # deferred, not answered or dropped


def test_exam_sim_results_wait_for_a_skipped_question_then_score_it(tmp_path):
    # Results score only once every question is answered: a skipped one comes back after the
    # rest, and the sheet can't be finished around it.
    with _client(tmp_path) as client:
        first, _ = _sim_current(client)
        client.post(f"/api/exam/sim/skip?question_id={first}")
        for _ in range(34):
            qid, _ = _sim_current(client)
            assert qid != first
            _sim_answer(client, qid, _pool_answer(qid))
        back, page = _sim_current(client)
        assert back == first
        assert "Question 1 of 35" in page.text
        assert ">Skip<" not in page.text                 # the last one left: nowhere to skip to
        early = client.get("/exam/sim/results", follow_redirects=False)
        assert early.headers["location"].endswith("/exam/sim")
        last = _sim_answer(client, first, _pool_answer(first))
        review = client.get(last.headers["location"])
        assert 'href="/exam/sim/results"' in review.text
        results = client.get("/exam/sim/results")
    assert "35 of 35" in results.text


def test_exam_sim_skip_for_an_unknown_question_is_404(tmp_path):
    with _client(tmp_path) as client:
        client.get("/exam/sim")
        resp = client.post("/api/exam/sim/skip?question_id=NOPE99")
    assert resp.status_code == 404


# --- C14: a pool the console cannot vouch for ships the trainer disabled ----------------------
# A wrong pool teaches wrong answers, so a pool that fails its correctness guard is never served,
# and a pool file that is missing, unreadable or not shaped like a pool turns the trainer off,
# not the whole console.

def _shipped_pool():
    return json.loads(exam.POOL_PATH.read_text(encoding="utf-8"))


def _short_pool(path):
    """300 of the 409 questions: the right shape, so it loads, but not the published pool."""
    data = _shipped_pool()
    data["questions"] = data["questions"][:300]
    path.write_text(json.dumps(data), encoding="utf-8")


def _pre_errata_pool(path):
    """All 409 questions, one still worded as before the 2026-02-19 errata: a stale pool."""
    data = _shipped_pool()
    for q in data["questions"]:
        if q["id"] == "T0A10":
            q["question"] = ("What hazard occurs when rapidly charging or discharging an "
                             "unprotected battery?")
    path.write_text(json.dumps(data), encoding="utf-8")


def _cut_off_pool(path):
    """The shipped pool cut off halfway through, as an interrupted write leaves it."""
    text = exam.POOL_PATH.read_text(encoding="utf-8")
    path.write_text(text[:len(text) // 2], encoding="utf-8")


def _empty_pool(path):
    path.write_text("", encoding="utf-8")


def _pool_without_an_answer(path):
    """One question has lost its answer field."""
    data = _shipped_pool()
    del data["questions"][0]["answer"]
    path.write_text(json.dumps(data), encoding="utf-8")


def _pool_holding_a_list(path):
    """Valid JSON, but a list where the pool object belongs."""
    path.write_text("[]", encoding="utf-8")


def _pool_with_a_question_that_is_not_an_object(path):
    data = _shipped_pool()
    data["questions"][0] = "T1A01"
    path.write_text(json.dumps(data), encoding="utf-8")


def _unreadable_pool(path):
    """The shipped pool, in a file nobody may read."""
    path.write_text(exam.POOL_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    path.chmod(0)


def _pool_that_is_a_directory(path):
    """A directory where the pool file belongs: it cannot be read as a file, even by root."""
    path.mkdir()


def _no_pool_file(path):
    """Nothing at the pool's path."""


def _pool_with_no_questions(path):
    path.write_text(json.dumps({"questions": []}), encoding="utf-8")


def _pool_file(tmp_path, write):
    path = tmp_path / "pool.json"
    write(path)
    return path


def _says_trainer_disabled(resp, reason):
    """The page says the trainer is disabled and gives the reason, as the learner reads it."""
    text = unescape(resp.text)
    return "trainer disabled" in text.lower() and reason in text


@pytest.mark.parametrize("write, reason", [
    (_short_pool, "pool has 300 questions, expected 409"),
    (_pre_errata_pool, "errata question T0A10 does not carry its corrected text"),
])
def test_a_pool_that_fails_its_check_is_not_served(tmp_path, write, reason):
    # Short or stale, the pool loads without error; only its check refuses it. The study card
    # must not serve a question from it, and must say why the trainer is off.
    with _client(tmp_path, pool_path=_pool_file(tmp_path, write)) as client:
        card = client.get("/exam")
    assert card.status_code == 503
    assert 'name="choice"' not in card.text
    assert _says_trainer_disabled(card, reason)


def test_a_missing_pool_file_does_not_stop_the_console(tmp_path):
    with _client(tmp_path, pool_path=tmp_path / "no-such-pool.json") as client:
        home = client.get("/")
        lesson = client.get("/lesson/1")
        card = client.get("/exam")
    assert home.status_code == 200
    assert lesson.status_code == 200
    assert card.status_code == 503
    assert _says_trainer_disabled(card, "pool file not found")


@pytest.mark.parametrize("write, reason", [
    (_cut_off_pool, "pool file is malformed: "),
    (_empty_pool, "pool file is malformed: Expecting value"),
    (_pool_without_an_answer, "pool file is malformed: a question has no 'answer' field"),
    (_pool_holding_a_list, "pool file is malformed: "),
    (_pool_with_a_question_that_is_not_an_object, "pool file is malformed: "),
    # Root reads a file whatever its mode, so this case means nothing when the suite runs as root.
    pytest.param(_unreadable_pool, "pool file cannot be read: Permission denied",
                 marks=pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")),
    # The same error path for every user, root included.
    (_pool_that_is_a_directory, "pool file cannot be read: Is a directory"),
])
def test_a_broken_pool_file_does_not_stop_the_console(tmp_path, write, reason):
    with _client(tmp_path, pool_path=_pool_file(tmp_path, write)) as client:
        home = client.get("/")
        lesson = client.get("/lesson/1")
        card = client.get("/exam")
    assert home.status_code == 200
    assert lesson.status_code == 200
    assert card.status_code == 503
    assert _says_trainer_disabled(card, reason)


# Every exam route, each with what it would act on if the trainer were on. The restart comes
# before the practice exam, so a sim the routes start would still be there at the end.
_EXAM_ROUTES = (
    ("GET", "/exam"),
    ("POST", "/api/exam/answer?question_id=T1A01"),
    ("GET", "/exam/review/T1A01?choice=A"),
    ("POST", "/api/exam/sim/restart"),
    ("GET", "/exam/sim"),
    ("POST", "/api/exam/sim/answer?question_id=T1A01"),
    ("POST", "/api/exam/sim/skip?question_id=T1A01"),
    ("GET", "/exam/sim/review/T1A01"),
    ("GET", "/exam/sim/results"),
)


def test_every_exam_route_says_the_trainer_is_disabled_and_why(tmp_path):
    # A refused pool is refused on every route, not only on the card: no question, no answer
    # recorded and no practice exam drawn from it, and each screen says why.
    reason = "pool has 300 questions, expected 409"
    with _client(tmp_path, pool_path=_pool_file(tmp_path, _short_pool)) as client:
        answers = {}
        for method, url in _EXAM_ROUTES:
            body = {"data": {"choice": "A"}, "headers": _FORM} if method == "POST" else {}
            resp = client.request(method, url, follow_redirects=False, **body)
            answers[url] = (resp.status_code, _says_trainer_disabled(resp, reason))
        recorded = client.app.state.exam_store.state("T1A01")
        sim = client.app.state.exam_store.sim()
    assert answers == {url: (503, True) for _, url in _EXAM_ROUTES}
    assert recorded is None
    assert sim is None


def _ask_every_exam_route(client):
    """Each exam route's answer, in _EXAM_ROUTES order; the posts carry a choice."""
    pages = []
    for method, url in _EXAM_ROUTES:
        body = {"data": {"choice": "A"}, "headers": _FORM} if method == "POST" else {}
        pages.append(client.request(method, url, follow_redirects=False, **body))
    return pages


# An absolute path in shown text: a token that starts at the root ("/tmp/...", "/home/..."), at
# a home folder ("~/...", "~user"), or at a Windows drive ("C:\...").
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s'\"(=:])(?:/[^\s/]|~)|\b[A-Za-z]:\\")


# Every refused pool. The directory stands for every file that cannot be read: an OSError shows
# only its strerror, whatever the cause.
@pytest.mark.parametrize("write", [
    _no_pool_file, _pool_with_no_questions, _short_pool, _pre_errata_pool, _cut_off_pool,
    _empty_pool, _pool_without_an_answer, _pool_holding_a_list,
    _pool_with_a_question_that_is_not_an_object, _pool_that_is_a_directory,
])
def test_no_exam_page_shows_where_the_pool_lives(tmp_path, write):
    # The reason is on the learner's screen, and on the kiosk the pool's absolute path runs
    # through the home folder. A reason may name the file, never the folders above it.
    with _client(tmp_path, pool_path=_pool_file(tmp_path, write)) as client:
        pages = _ask_every_exam_route(client)
    for (_, url), page in zip(_EXAM_ROUTES, pages):
        found = re.search(r'class="error-screen__detail">(.*?)</p>', page.text, re.S)
        assert found, url
        reason = unescape(found.group(1)).strip()
        assert reason, url
        assert not _ABSOLUTE_PATH.search(reason), (url, reason)
        assert str(tmp_path) not in reason, (url, reason)
        assert "/home/" not in reason, (url, reason)


def test_a_refused_pool_is_logged_with_its_reason(tmp_path):
    with _client(tmp_path, pool_path=_pool_file(tmp_path, _short_pool)):
        pass
    logged = [e["payload"] for e in read_events(tmp_path / "events.jsonl")
              if e["kind"] == "exam_disabled"]
    assert logged == [{"reason": "pool has 300 questions, expected 409"}]


def test_a_refused_pool_is_logged_once_however_often_the_trainer_is_asked(tmp_path):
    # The reason is logged at start-up, not again on each refused request: a learner tapping the
    # exam bar again and again must not grow the log by a line each time.
    with _client(tmp_path, pool_path=_pool_file(tmp_path, _short_pool)) as client:
        for _ in range(3):
            _ask_every_exam_route(client)
    logged = [e["payload"] for e in read_events(tmp_path / "events.jsonl")
              if e["kind"] == "exam_disabled"]
    assert logged == [{"reason": "pool has 300 questions, expected 409"}]


# An OSError need not carry a strerror: one raised with free text only, or with no text at all,
# has none, and its free text may hold the pool's whole path.
@pytest.mark.parametrize("error", [
    lambda path: OSError(f"short read on {path}"),
    lambda path: OSError(),
    lambda path: TimeoutError(f"timed out reading {path}"),
], ids=["text-naming-the-path", "no-text", "subclass-naming-the-path"])
def test_an_unreadable_pool_without_a_strerror_still_says_so_and_names_no_folder(
        tmp_path, monkeypatch, error):
    # The reason must still read as a sentence (no colon left dangling with nothing after it)
    # and must name no folder, on every exam page and in the log alike.
    pool = tmp_path / "pool.json"

    def refuse(path=None):
        raise error(pool)

    monkeypatch.setattr("sigint101.app.load_pool", refuse)
    with _client(tmp_path, pool_path=pool) as client:
        pages = _ask_every_exam_route(client)
    logged = [e["payload"]["reason"] for e in read_events(tmp_path / "events.jsonl")
              if e["kind"] == "exam_disabled"]
    assert len(logged) == 1
    reason = logged[0]
    for (_, url), page in zip(_EXAM_ROUTES, pages):
        assert page.status_code == 503, url
        found = re.search(r'class="error-screen__detail">(.*?)</p>', page.text, re.S)
        assert found, url
        assert unescape(found.group(1)).strip() == reason.strip(), url
    assert reason.startswith("pool file cannot be read"), reason
    assert not reason.rstrip().endswith(":"), reason
    assert "None" not in reason, reason
    assert not _ABSOLUTE_PATH.search(reason), reason
    assert str(tmp_path) not in reason, reason
    assert "/home/" not in reason, reason


# --- A44: the home screen's exam bar while the trainer is off --------------------------------
# The bar stays a link, so the reason stays one tap away. While the pool stands refused its
# subtitle says so, in the refusal page's own words, instead of inviting the learner to practise.

def _exam_bar(home):
    """The home screen's link to the trainer: its title and subtitle as the learner reads them."""
    link = re.search(r'<a\b[^>]*\bhref="/exam"[^>]*>(.*?)</a>', home.text, re.S)
    assert link, "home links to the trainer"

    def words(part):
        found = re.search(rf'class="[^"]*\bexam-cta__{part}\b[^"]*"[^>]*>(.*?)</span>',
                          link.group(1), re.S)
        assert found, f"the bar's {part} sits inside its link"
        return " ".join(unescape(found.group(1)).split())

    return words("title"), words("sub")


def test_home_exam_bar_invites_study_while_the_pool_is_good(tmp_path):
    with _client(tmp_path) as client:
        home = client.get("/")
        card = client.get("/exam")
    assert card.status_code == 200          # the shipped pool passes its check: the trainer is on
    assert _exam_bar(home) == ("Licence exam", "Technician study")
    assert "practice" not in _exam_bar(home)[1].lower()
    assert "trainer disabled" not in unescape(home.text).lower()


@pytest.mark.parametrize("write", [_short_pool, _no_pool_file, _pool_that_is_a_directory])
def test_home_exam_bar_says_trainer_disabled_while_the_pool_is_refused(tmp_path, write):
    with _client(tmp_path, pool_path=_pool_file(tmp_path, write)) as client:
        home = client.get("/")
        card = client.get("/exam")
    assert home.status_code == 200
    assert card.status_code == 503
    heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", card.text, re.S)
    shown = " ".join(unescape(heading.group(1)).split())
    assert shown == "Trainer disabled"
    # The bar is still the link in, and its subtitle is the refusal page's heading, word for word.
    assert _exam_bar(home) == ("Licence exam", shown)
    assert "technician practice" not in unescape(home.text).lower()


# --- provenance panel (M3): the radio's account of itself + baseline (deaf vs. empty) ---------


def test_provenance_reports_the_radios_account_of_itself(tmp_path):
    with _client(tmp_path, sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.get("/api/provenance")
    assert resp.status_code == 200
    body = resp.json()
    assert "provenance" in body and "baseline" in body
    # The sweep covers the reference (106.5 MHz), so its level is a real reading, not None.
    assert body["provenance"]["ref_db"] is not None
    # Nothing captured yet, so there is no baseline to judge the radio against.
    assert body["baseline"]["has_baseline"] is False


def test_capturing_a_baseline_then_comparing_reads_not_degraded(tmp_path):
    with _client(tmp_path, sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        cap = client.post("/api/provenance/baseline")
        assert cap.status_code == 200 and cap.json()["recorded"] is True
        resp = client.get("/api/provenance")
    baseline = resp.json()["baseline"]
    assert baseline["has_baseline"] is True
    # The reference has not dropped against the baseline it was just taken from.
    assert baseline["degraded"] is False


def test_baseline_capture_without_a_reference_is_refused(tmp_path):
    # No radio means no reference level, so there is nothing honest to record a baseline from.
    with _client(tmp_path, probe=_absent) as client:
        resp = client.post("/api/provenance/baseline")
    assert resp.status_code == 400
    assert resp.json()["recorded"] is False


class _FmSweep(_IdleProc):
    """A launched rtl_power at the process boundary, printing a real capture of the FM band."""

    def __init__(self):
        self.stdout = iter(FM_LINES)


def _surveyed_sweeper(serial):
    """A real Sweeper on a bus holding one radio with this serial, sweeping the captured band."""
    return Sweeper(spawn=lambda cmd: _FmSweep(), radio_present=lambda: True,
                   list_devices=lambda: (RtlSdrDevice(0, serial),))


def _read_with_rows(client):
    """Start the sweep, let its captured rows land, and read the provenance again."""
    client.get("/api/provenance")
    client.app.state.sweeper.drain()
    return client.get("/api/provenance").json()


def _setup(**changes):
    """The set-up a baseline sample counts for: serial, gain, reference station, bin, span."""
    return {"serial": "AAAA", "gain": None, "ref_hz": REFERENCE_HZ, "bin_size": DEFAULT_BIN,
            "freq_low": DEFAULT_LOW, "freq_high": DEFAULT_HIGH, **changes}


def test_the_baseline_is_only_the_samples_taken_under_the_current_set_up(tmp_path):
    # Another radio, two gain steps, another bin, station or span can move the reference level
    # past the threshold. Pooled, these samples 15 dB up would call this healthy radio deaf.
    with _client(tmp_path, sweeper_factory=lambda: _surveyed_sweeper("AAAA")) as client:
        level = _read_with_rows(client)["provenance"]["ref_db"]
        events = tmp_path / "events.jsonl"
        for _ in range(2):
            log_event(events, "baseline_sample", {**_setup(), "ref_db": level})
        for other in (_setup(serial="BBBB"), _setup(gain=20.7), _setup(bin_size=62_500),
                      _setup(ref_hz=94_700_000),
                      _setup(freq_low=89_037_500, freq_high=109_037_500)):
            log_event(events, "baseline_sample", {**other, "ref_db": level + 15})
        baseline = client.get("/api/provenance").json()["baseline"]
    assert baseline["n_samples"] == 2
    assert baseline["baseline_ref_db"] == level
    assert baseline["degraded"] is False
    assert baseline["n_set_aside"] == 5


def test_a_sample_logged_before_set_ups_were_recorded_counts_for_no_set_up(tmp_path):
    # The shape every sample had before: no bin size, and the serial an unset environment gave
    # (null). Every field it records agrees with this radio-less stand-in's set-up, yet which
    # radio read it is unknown, so it is set aside and the learner captures a fresh baseline.
    old = {"serial": None, "gain": None, "bias_state": BIAS_STATE, "soc_temp_c": 41.0,
           "throttled": None, "ref_hz": REFERENCE_HZ}
    with _client(tmp_path, sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        level = client.get("/api/provenance").json()["provenance"]["ref_db"]
        for _ in range(2):
            log_event(tmp_path / "events.jsonl", "baseline_sample", {**old, "ref_db": level + 15})
        baseline = client.get("/api/provenance").json()["baseline"]
    assert baseline == {"has_baseline": False, "n_set_aside": 2}


def test_a_captured_sample_records_the_surveyed_serial_and_the_settings(tmp_path, monkeypatch):
    # The shipped unit sets no serial in its environment: the serial is the string the bus survey
    # listed for the radio the sweep opened, whatever the dongle reports.
    monkeypatch.delenv("SIGINT101_RTLSDR_SERIAL", raising=False)
    with _client(tmp_path, sweeper_factory=lambda: _surveyed_sweeper("AAAA")) as client:
        _read_with_rows(client)
        assert client.post("/api/provenance/baseline").json()["recorded"] is True
    [sample] = read_baseline(tmp_path / "events.jsonl")
    assert sample["serial"] == "AAAA"
    assert sample["bin_size"] == DEFAULT_BIN
    assert (sample["gain"], sample["ref_hz"]) == (None, REFERENCE_HZ)
    assert (sample["freq_low"], sample["freq_high"]) == (DEFAULT_LOW, DEFAULT_HIGH)


def _fed_sweeper(procs, bus, now):
    """A real Sweeper whose rtl_power prints only what the test feeds procs[-1].

    `bus` holds the serials the survey finds, and a test may swap them; `now` is the clock the
    restart back-off runs on.
    """
    def spawn(cmd):
        procs.append(FedProc())
        return procs[-1]
    return Sweeper(spawn=spawn, radio_present=lambda: bool(bus),
                   list_devices=lambda: tuple(RtlSdrDevice(i, s) for i, s in enumerate(bus)),
                   clock=lambda: now[0])


def _shifted(lines, db):
    """The captured sweep with every level moved by `db`: a stand-in for the level at another
    gain or through another radio, whose real size was not measured."""
    out = []
    for line in lines:
        fields = [field.strip() for field in line.split(",")]
        out.append(", ".join(fields[:6] + [f"{float(v) + db:.2f}" for v in fields[6:] if v]))
    return out


def _logged_and_deaf(tmp_path, polls):
    """The (serial, gain) of every sample logged, and which polls called the radio deaf."""
    logged = [(s["serial"], s["gain"]) for s in read_baseline(tmp_path / "events.jsonl")]
    return logged, [name for name, body in polls.items() if body["baseline"].get("degraded")]


def test_after_a_gain_step_nothing_is_read_or_captured_until_the_new_gain_has_swept(tmp_path):
    # The old gain's rows stay for the waterfall, but none may be read, or captured, under the
    # new gain's name. Until the restarted sweep has finished a pass the panel has no reference
    # and Capture is refused; then the new gain has no baseline of its own; and back at the
    # captured gain, its own fresh rows are read, never the other gain's.
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _fed_sweeper(procs, bus, now)) as client:
        sweeper = client.app.state.sweeper
        client.get("/api/provenance")
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        assert client.post("/api/provenance/baseline").status_code == 200
        assert client.post("/api/gain", json={"gain": 0.0}).json()["set"] is True
        polls = {"stepped": client.get("/api/provenance").json()}
        refused = client.post("/api/provenance/baseline")
        procs[-1].feed(_shifted(FM_LINES, -20))
        wait_for_rows(sweeper, 4)
        polls["swept at 0 dB"] = client.get("/api/provenance").json()
        client.post("/api/gain", json={"gain": None})
        polls["back at auto"] = client.get("/api/provenance").json()
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 6)
        polls["swept at auto"] = client.get("/api/provenance").json()
    assert _logged_and_deaf(tmp_path, polls) == ([("AAAA", None)], [])
    assert polls["stepped"]["provenance"]["ref_db"] is None
    assert (refused.status_code, refused.json()["recorded"]) == (400, False)
    assert "has not finished a pass" in refused.json()["reason"]
    assert polls["swept at 0 dB"]["baseline"] == {"has_baseline": False, "n_set_aside": 1}
    assert polls["back at auto"]["provenance"]["ref_db"] is None
    assert polls["swept at auto"]["baseline"]["n_samples"] == 1


def test_after_a_radio_swap_nothing_is_read_or_captured_until_the_new_radio_has_swept(tmp_path):
    # Radio AAAA is pulled and its rtl_power exits (modelled here), and BBBB is plugged in. AAAA's
    # rows stay in the buffer, but none may be read, or captured, under BBBB's serial. This is
    # also the old row served after a replug: the first poll that reopens a radio has no reading.
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, sweeper_factory=lambda: _fed_sweeper(procs, bus, now)) as client:
        sweeper = client.app.state.sweeper
        client.get("/api/provenance")
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        assert client.post("/api/provenance/baseline").status_code == 200
        procs[-1].die(1)
        sweeper.drain()
        bus[:] = ["BBBB"]
        now[0] += 120                        # past the restart back-off
        polls = {"death reported": client.get("/api/provenance").json(),
                 "BBBB opened": client.get("/api/provenance").json()}
        refused = client.post("/api/provenance/baseline")
        procs[-1].feed(_shifted(FM_LINES, -12))
        wait_for_rows(sweeper, 4)
        polls["BBBB swept"] = client.get("/api/provenance").json()
    assert _logged_and_deaf(tmp_path, polls) == ([("AAAA", None)], [])
    assert polls["BBBB opened"]["provenance"]["serial"] == "BBBB"
    assert polls["BBBB opened"]["provenance"]["ref_db"] is None
    assert refused.status_code == 400
    assert polls["BBBB swept"]["provenance"]["serial"] == "BBBB"
    assert polls["BBBB swept"]["baseline"] == {"has_baseline": False, "n_set_aside": 1}


def test_one_tune_step_sets_the_baseline_aside_until_the_span_is_back(tmp_path):
    # Lesson 3 has a tune strip. One + tap shifts the span, and with it where the reference falls
    # in its hop, which can move its level by several dB with nothing wrong. A sample from
    # another span is set aside; back on the captured span it counts again.
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _surveyed_sweeper("AAAA")) as client:
        _read_with_rows(client)
        assert client.post("/api/provenance/baseline").status_code == 200
        plus = {"center": 98_100_000, "span": 20_000_000, "bin_size": DEFAULT_BIN}
        assert client.post("/api/tune", json=plus).json()["tuned"] is True
        tuned = _read_with_rows(client)
        home = {"low": DEFAULT_LOW, "high": DEFAULT_HIGH, "bin_size": DEFAULT_BIN}
        assert client.post("/api/tune", json=home).json()["tuned"] is True
        back = _read_with_rows(client)
    assert tuned["baseline"] == {"has_baseline": False, "n_set_aside": 1}
    assert tuned["provenance"]["ref_db"] is not None
    assert (back["baseline"]["has_baseline"], back["baseline"]["n_samples"]) == (True, 1)


def test_a_sweep_that_stops_without_exiting_reads_no_reference_until_it_sweeps_again(tmp_path):
    # rtl_power can stop sweeping without exiting. It still reads as running, and its last row
    # stayed the reading: a saved baseline kept the panel at HEALTHY on a level that only grew old.
    # Past the age limit there is no reference, so the panel reads NO REFERENCE before any baseline
    # verdict, and Capture is refused with the reason. The waterfall keeps its rows, and the first
    # pass swept wholly after the stall brings the reading back.
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _fed_sweeper(procs, bus, now)) as client:
        sweeper = client.app.state.sweeper
        client.get("/api/provenance")
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        assert client.post("/api/provenance/baseline").status_code == 200
        now[0] += 60                        # a minute with no new pass from a live rtl_power
        assert sweeper.running
        stalled = client.get("/api/provenance").json()
        refused = client.post("/api/provenance/baseline")
        rows = client.get("/api/spectrum").json()["rows"]
        # The first pass after the stall publishes the one held back when it stalled, and the
        # second the one rtl_power was sweeping then; both carry levels from before the stall.
        # The third brings the reading back.
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 5)
        resumed = client.get("/api/provenance").json()
    assert stalled["provenance"]["ref_db"] is None
    assert (stalled["baseline"]["has_baseline"], stalled["baseline"]["degraded"]) == (True, False)
    assert (refused.status_code, refused.json()["recorded"]) == (400, False)
    assert refused.json()["reason"] == "no reference — the sweep has not finished a pass for 60 s"
    assert len(rows) == 2
    assert resumed["provenance"]["ref_db"] is not None
    assert len(read_baseline(tmp_path / "events.jsonl")) == 1


def test_a_stalled_sweep_and_a_restarted_one_refuse_capture_each_with_its_own_reason(tmp_path):
    # A stall and a restart both leave no reference, for different reasons, and the refusal names
    # the right one: a restart (here a gain step) waits for its own first pass, as C12 made it.
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _fed_sweeper(procs, bus, now)) as client:
        sweeper = client.app.state.sweeper
        client.get("/api/provenance")
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        now[0] += 60
        stalled = client.post("/api/provenance/baseline")
        assert client.post("/api/gain", json={"gain": 0.0}).json()["set"] is True
        client.get("/api/provenance")       # the poll that restarts the sweep at the new gain
        restarted = client.post("/api/provenance/baseline")
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 4)
        swept = client.post("/api/provenance/baseline")
    assert (stalled.status_code, restarted.status_code, swept.status_code) == (400, 400, 200)
    assert "has not finished a pass for 60 s" in stalled.json()["reason"]
    assert "has not finished a pass since it started" in restarted.json()["reason"]
    assert [sample["gain"] for sample in read_baseline(tmp_path / "events.jsonl")] == [0.0]


def test_after_a_stall_capture_is_refused_until_a_pass_swept_wholly_after_it(tmp_path):
    # The first two rows published after a stall carry levels from before it: the pass held back
    # when rtl_power stalled, then the pass it was sweeping then. Capture logged the first of them.
    # Both are no reference now, and Capture says why; the third row is the reference again.
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _fed_sweeper(procs, bus, now)) as client:
        sweeper = client.app.state.sweeper
        client.get("/api/provenance")
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        now[0] += 60                        # a minute with no new pass from a live rtl_power
        refused = []
        for n in range(2):
            procs[-1].feed(_fm_pass(n))
            wait_for_rows(sweeper, 3 + n)
            refused.append(client.post("/api/provenance/baseline"))
        procs[-1].feed(_fm_pass(2))
        wait_for_rows(sweeper, 5)
        captured = client.post("/api/provenance/baseline")
    assert [response.status_code for response in refused] == [400, 400]
    assert all("after a stall" in response.json()["reason"] for response in refused)
    assert captured.status_code == 200
    assert len(read_baseline(tmp_path / "events.jsonl")) == 1


def test_the_spectrum_route_marks_a_stall_by_the_rule_the_reading_follows(tmp_path):
    # A stalled rtl_power still answers with its kept rows, and the waterfall drew them as live.
    # The route still serves them (they are real, and Lesson 2 compares gains on them), but says
    # the sweep has stalled, past the limit, and then that it is resuming, for exactly as long as
    # the baseline panel has no reading; it clears with the reading.
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _fed_sweeper(procs, bus, now)) as client:
        sweeper = client.app.state.sweeper

        def poll():
            spectrum = client.get("/api/spectrum").json()
            reference = client.get("/api/provenance").json()["provenance"]["ref_db"]
            return (spectrum["available"], len(spectrum["rows"]), spectrum.get("stale"),
                    reference is not None)

        client.get("/api/spectrum")
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        polls = [poll()]
        now[0] += sweeper.stale_after() - 0.01
        polls.append(poll())
        now[0] += 0.02                      # just past the limit
        polls.append(poll())
        for n in range(3):
            procs[-1].feed(_fm_pass(n))
            wait_for_rows(sweeper, 3 + n)
            polls.append(poll())
    assert polls == [(True, 2, None, True), (True, 2, None, True), (True, 2, "stalled", False),
                     (True, 3, "resuming", False), (True, 4, "resuming", False),
                     (True, 5, None, True)]


@pytest.mark.parametrize("path,payload,restart_first", [
    pytest.param("/api/gain", {"gain": 29.7}, False, id="gain"),
    pytest.param("/api/tune", {"low": DEFAULT_LOW, "high": DEFAULT_HIGH, "bin_size": DEFAULT_BIN},
                 False, id="same-span-retune"),
    pytest.param(None, None, False, id="crash"),
    pytest.param("/api/ppm/measure", None, False, id="ppm"),
    pytest.param("/api/gain", {"gain": 29.7}, True, id="restart-then-gain"),
])
def test_spectrum_waits_then_stalls_when_the_current_sweeps_first_row_is_late(
    tmp_path, path, payload, restart_first
):
    procs, bus, now = [], ["AAAA"], [1000.0]

    def measure(**kw):
        return {"hz": kw["hz"], "ppm": 10.0, "reason": ""}

    with _client(tmp_path, probe=_present, ppm_measure=measure,
                 sweeper_factory=lambda: _fed_sweeper(procs, bus, now)) as client:
        sweeper = client.app.state.sweeper
        published = _flush_event(sweeper)

        def poll():
            response = client.get("/api/spectrum")
            assert response.status_code == 200
            body = response.json()
            assert body["available"] is True
            return body

        starting = poll()
        _feed_row(procs[-1], FM_LINES[:9], published)
        _feed_row(procs[-1], FM_LINES[9:], published)
        before = poll()
        assert before["stale"] is None
        if restart_first:
            now[0] += sweeper.stale_after() + 1
            assert client.post("/api/spectrum/restart").status_code == 200
            assert poll()["stale"] == "restarting"
        if path is None:
            procs[-1].die(1)
            sweeper.drain()
            now[0] += 60  # past back-off; report the death once before starting again
            assert client.get("/api/spectrum").status_code == 503
        else:
            assert client.post(path, json=payload).status_code == 200

        waiting = poll()
        bound = sweeper.stale_after() + sweeper.START_ALLOWANCE_S
        now[0] += bound - 0.01
        within_allowance = poll()
        now[0] += 0.02
        stalled = poll()
        stalled_capture = client.post("/api/provenance/baseline")
        kept = [] if path in ("/api/tune", "/api/ppm/measure") else before["rows"]
        after_stall, captures = [], []
        for lines in (FM_LINES[:9], FM_LINES[9:17], FM_LINES[17:] + FM_LINES[:1]):
            _feed_row(procs[-1], lines, published)
            after_stall.append(poll())
            captures.append(client.post("/api/provenance/baseline"))

    assert (starting["stale"], starting["rows"]) == ("waiting", [])
    assert (waiting["stale"], within_allowance["stale"], stalled["stale"]) == (
        "waiting", "waiting", "stalled")
    assert waiting["rows"] == within_allowance["rows"] == stalled["rows"] == kept
    assert stalled_capture.status_code == 400
    assert "has not finished a pass for" in stalled_capture.json()["reason"]
    assert [body["stale"] for body in after_stall] == ["resuming", "resuming", None]
    assert [len(body["rows"]) for body in after_stall] == [len(kept) + n for n in (1, 2, 3)]
    assert after_stall[-1]["rows"][:len(kept)] == kept
    assert [response.status_code for response in captures] == [400, 400, 200]
    assert all("after a stall" in response.json()["reason"] for response in captures[:2])
    assert len(read_baseline(tmp_path / "events.jsonl")) == 1


# --- ppm panel (M4): measure the crystal error against a broadcast stereo pilot ---------------


def test_ppm_measure_reports_the_measurement(tmp_path):
    # The measure is injected (the real one captures 8s of IQ); the route stops the sweeper first
    # because only one process may hold the dongle, then hands back what the measurement found.
    def fake_measure(**kw):
        return {"hz": kw["hz"], "ppm": 164.3, "reason": "", "pilot_ratio_db": 22.0,
                "soc_temp_c": 41.0, "resolution_ppm": 0.5}
    with _client(tmp_path, ppm_measure=fake_measure,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.post("/api/ppm/measure?hz=98100000")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ppm"] == 164.3
    assert body["pilot_ratio_db"] == 22.0


def test_ppm_measure_without_a_pilot_is_honest(tmp_path):
    # No 19 kHz pilot means no measurement — reported as ppm=None with the reason, never guessed.
    def fake_measure(**kw):
        return {"hz": kw["hz"], "ppm": None,
                "reason": "no 19 kHz pilot here — a mono station carries none"}
    with _client(tmp_path, ppm_measure=fake_measure,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.post("/api/ppm/measure?hz=98100000")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ppm"] is None
    assert "pilot" in body["reason"]


def test_ppm_measure_releases_the_radio_before_measuring(tmp_path):
    # rtl_fm cannot open the dongle while the sweep holds it, so the sweeper must stand down first.
    seen = {}

    def fake_measure(**kw):
        seen["stopped_before_measure"] = not client_sweeper["running"]
        return {"hz": kw["hz"], "ppm": 10.0, "reason": ""}

    client_sweeper = {"running": True}

    class _RecordingSweeper(_StubSweeper):
        def stop(self):
            client_sweeper["running"] = False
            super().stop()

        def release(self):
            # The real Sweeper's release() stops the sweep and keeps it down until resumed.
            self.stop()
            super().release()

    with _client(tmp_path, ppm_measure=fake_measure,
                 sweeper_factory=lambda: _RecordingSweeper(_rows())) as client:
        client.get("/api/spectrum")  # start the sweep so there is something to stand down
        client_sweeper["running"] = True
        client.post("/api/ppm/measure?hz=98100000")
    assert seen.get("stopped_before_measure") is True


# --- C24: the Restart sweep key's route ---------------------------------------------------------
# WHY: nothing ended an rtl_power that stopped sweeping without exiting. The waterfall's Restart
# sweep key, shown only while it says STALLED, posts here. A press on a stalled sweep stops it and
# keeps its rows; the next poll starts a new run, which reads "restarting" until its first row. A
# press on a sweep that is not stalled changes nothing, and never takes the radio back from another
# tool. The answer carries the reason the old run had recorded, so it is never lost entirely.

def _first_fed_sweeper(first, procs, bus, now):
    """_fed_sweeper whose first rtl_power is a `first`; every later start gets a FedProc."""
    def spawn(cmd):
        procs.append(first() if not procs else FedProc())
        return procs[-1]
    sweeper = Sweeper(spawn=spawn, radio_present=lambda: bool(bus),
                      list_devices=lambda: tuple(RtlSdrDevice(i, s) for i, s in enumerate(bus)),
                      clock=lambda: now[0])
    sweeper.STOP_WAIT_S = 0.05
    return sweeper


def _kinds(tmp_path, kind):
    return [e["payload"] for e in read_events(tmp_path / "events.jsonl") if e["kind"] == kind]


def test_the_restart_route_restarts_a_stalled_sweep_end_to_end(tmp_path):
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _fed_sweeper(procs, bus, now)) as client:
        sweeper = client.app.state.sweeper
        client.get("/api/spectrum")
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        now[0] += 60
        stalled = client.get("/api/spectrum").json()
        pressed = client.post("/api/spectrum/restart")
        after = client.get("/api/spectrum").json()
        refused = client.post("/api/provenance/baseline")
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 4)
        live = client.get("/api/spectrum").json()
        captured = client.post("/api/provenance/baseline")
    assert stalled["stale"] == "stalled"
    assert (pressed.status_code, pressed.json()) == (
        200, {"restarted": True, "stalled_for_s": 60.0, "error": None})
    assert (after["stale"], len(after["rows"]), len(procs)) == ("restarting", 2, 2)
    assert after["command"] and procs[0].terminated
    assert refused.status_code == 400
    assert "has not finished a pass since it started" in refused.json()["reason"]
    assert live["stale"] is None and captured.status_code == 200
    assert _kinds(tmp_path, "spectrum_restart") == [{"stalled_for_s": 60.0, "error": None}]


@contextmanager
def _not_stalled_console(tmp_path, case):
    """A console whose sweep is in a state a press must leave alone, and the state to name."""
    procs, bus, now = [], ([] if case == "no radio" else ["AAAA"]), [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _fed_sweeper(procs, bus, now),
                 decoder_factory=lambda: _StubDecoder(_reads())) as client:
        response = client.get("/api/spectrum")
        if case == "no radio":
            assert response.status_code == 503
            yield client, procs, "stopped"
            return
        sweeper = client.app.state.sweeper
        procs[-1].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        if case == "rtl_433":
            client.get("/api/decode")               # rtl_433 takes the dongle
            state = "released"
        elif case == "resuming":
            now[0] += 60
            procs[-1].feed(_fm_pass(0))
            wait_for_rows(sweeper, 3)
            state = "resuming"
        elif case == "second press":
            now[0] += 60
            assert client.post("/api/spectrum/restart").status_code == 200  # the first press
            state = "restarting"
        else:
            state = "live"
        yield client, procs, state


def test_restart_case_closes_its_client_if_setup_fails(tmp_path, monkeypatch):
    clients, loops = [], []
    make_client = _client

    def capture_client(*args, **kwargs):
        client = make_client(*args, **kwargs)
        clients.append(client)
        return client

    def fail_wait(*args):
        [client] = clients
        loops.append(client.portal.call(asyncio.get_running_loop))
        raise RuntimeError("sweep setup failed")

    monkeypatch.setattr("tests.test_app._client", capture_client)
    monkeypatch.setattr("tests.test_app.wait_for_rows", fail_wait)
    try:
        with pytest.raises(RuntimeError, match="sweep setup failed"):
            with _not_stalled_console(tmp_path, "live"):
                pass
        [loop] = loops
        assert loop.is_closed()
    finally:
        for client in clients:
            if client.portal is not None:
                client.__exit__(None, None, None)


@pytest.mark.parametrize("case", ["live", "resuming", "second press", "rtl_433", "no radio"])
def test_the_restart_route_changes_nothing_when_the_sweep_is_not_stalled(tmp_path, case):
    with _not_stalled_console(tmp_path, case) as (client, procs, state):
        sweeper, decoder = client.app.state.sweeper, client.app.state.decoder
        before = (len(procs), [p.terminated for p in procs], len(sweeper.rows()),
                  sweeper.released, decoder.running, decoder._released)
        pressed = client.post("/api/spectrum/restart")
        after = (len(procs), [p.terminated for p in procs], len(sweeper.rows()),
                 sweeper.released, decoder.running, decoder._released)
    assert pressed.status_code == 409
    assert pressed.json()["restarted"] is False and pressed.json()["state"] == state
    assert after == before
    if case == "rtl_433":
        assert after[3:] == (True, True, False)     # the sweep stays released, rtl_433 runs on
    assert _kinds(tmp_path, "spectrum_restart_refused")[-1] == {"state": state}


def test_a_restart_that_cannot_free_the_dongle_says_so_and_the_next_poll_reports_it(tmp_path):
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _first_fed_sweeper(HeldProc, procs, bus, now)) as client:
        sweeper = client.app.state.sweeper
        client.get("/api/spectrum")
        procs[0].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        now[0] += 60
        pressed = client.post("/api/spectrum/restart")
        polled = client.get("/api/spectrum")
        procs[0].free()
    held = "rtl_power survived kill; the dongle may still be held"
    assert (pressed.status_code, pressed.json()["error"]) == (200, held)
    assert (polled.status_code, polled.json()["reason"]) == (503, held)


def test_the_restart_route_runs_in_the_threadpool(tmp_path):
    # A stop can take 9 s. An async route would run it on the event loop and freeze every page.
    with _client(tmp_path) as client:
        route = next(r for r in client.app.routes
                     if getattr(r, "path", None) == "/api/spectrum/restart"
                     and "POST" in r.methods)
    assert not inspect.iscoroutinefunction(route.endpoint)


def test_a_reason_the_provenance_poll_uses_up_is_kept_in_the_restart_log(tmp_path):
    # Lesson 3 and the rack's Health view poll the provenance too, and that poll starts the sweep
    # itself and swallows a refusal: it can use up the one-time report of why the old run failed
    # before the waterfall shows it. The press's answer and its log line keep the reason.
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _first_fed_sweeper(FailingPipeProc, procs, bus, now)
                 ) as client:
        sweeper = client.app.state.sweeper
        client.get("/api/spectrum")
        procs[0].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        procs[0].break_pipe(OSError("Input/output error"))
        sweeper.drain()
        now[0] += 60
        pressed = client.post("/api/spectrum/restart")
        client.get("/api/provenance")               # uses up the report
        polled = client.get("/api/spectrum")
    assert (pressed.status_code, pressed.json()["error"]) == (200, "Input/output error")
    assert (polled.status_code, polled.json()["stale"]) == (200, "restarting")
    assert _kinds(tmp_path, "spectrum_restart")[0]["error"] == "Input/output error"


def test_a_poll_that_starts_the_sweep_as_the_press_returns_cannot_take_its_reason(tmp_path):
    # A poll waiting on the lifecycle lock through the stop (up to 9 s) runs the moment restart()
    # lets go of it. If it is a provenance-style start, it reports the old run's reason once,
    # swallows it, and clears it. The press's answer and log line must still carry it, so the reason
    # is read under the same hold as the stop, not after. Forced: such a start runs right there.
    procs, bus, now = [], ["AAAA"], [1000.0]
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _first_fed_sweeper(FailingPipeProc, procs, bus, now)
                 ) as client:
        sweeper = client.app.state.sweeper
        client.get("/api/spectrum")
        procs[0].feed(FM_LINES)
        wait_for_rows(sweeper, 2)
        procs[0].break_pipe(OSError("Input/output error"))
        sweeper.drain()
        now[0] += 60
        press = sweeper.restart

        def press_then_a_waiting_poll_starts():
            pressed = press()
            try:
                sweeper.start()                     # as _reading() does: the refusal is swallowed
            except SpectrumUnavailable:
                pass
            return pressed

        sweeper.restart = press_then_a_waiting_poll_starts
        answer = client.post("/api/spectrum/restart")
        swallowed = sweeper.error
    assert swallowed is None                        # the poll did use the report up
    assert (answer.status_code, answer.json()["error"]) == (200, "Input/output error")
    assert _kinds(tmp_path, "spectrum_restart")[0]["error"] == "Input/output error"
