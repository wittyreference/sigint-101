# ABOUTME: Integration tests for the FastAPI console — its routes and the server-side
# ABOUTME: commit-before-reveal gate, driven through the TestClient against a fixture pack.

import re
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
from sigint101.decode import Decoder, parse_decode_line
from sigint101.dmr import DmrDecoder
from sigint101.flock import FlockDetector
from sigint101.hardware import RadioProbe
from sigint101.lora import LoRaListener, parse_lora_packet
from sigint101.pocsag import PocsagDecoder
from sigint101.rayhunter import RayhunterMonitor
from sigint101.rds import RDSDecoder, RDSStation, parse_rds_group
from sigint101.spectrum import Sweeper, SweepRow, validate_span
from sigint101.sstv import SstvDecoder

# The suite owns its lesson content: the real lesson_packs/ are authored in parallel, so a test
# that read them would go red on someone else's unfinished pack. These fixtures are stable.
FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"


def _present(detail="RTL2832U 0bda:2838 on USB bus"):
    """A probe result standing for a dongle physically on the bus."""
    return RadioProbe(True, "lsusb", detail)


def _absent(detail="no RTL2832U on USB bus"):
    """A probe result standing for an empty bus — the no-mock honest answer."""
    return RadioProbe(False, "lsusb", detail)


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


class _StubDecoder:
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
        '{"time":"2026-01-01 00:00:00","model":"Acurite-Tower","id":16262,"channel":"A",'
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


class _StubRDS:
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


class _StubAdsb:
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


class _StubAprs:
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


class _StubPocsag:
    """An app-boundary stand-in for the rtl_fm|multimon-ng PocsagDecoder (no real radio)."""

    def __init__(self, pages):
        self._pages = pages
        self._started = False
        self._released = False
        self.freq_hz = 929_600_000

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
    """One decoded pager page, in the dict shape PocsagDecoder.pages() hands the route."""
    return [{
        "baud": 1200, "address": 1234567, "function": 3,
        "kind": "alpha", "text": "CALL EXT 2210 URGENT",
    }]


class _StubAcars:
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


class _StubAis:
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


class _StubDmr:
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


class _StubApt:
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


class _StubSstv:
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


class _StubFlock:
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
    return {"model": "Flipper Zero", "name": "Lle0zik", "firmware": "1.4.3",
            "region": "US", "radio_alive": True}


def _client(tmp_path, *, probe=None, sweeper_factory=None, ppm_measure=None,
            decoder_factory=None, lora_factory=None, rds_factory=None, adsb_factory=None,
            aprs_factory=None, pocsag_factory=None, acars_factory=None, ais_factory=None,
            dmr_factory=None, apt_factory=None, sstv_factory=None, flock_factory=None,
            rayhunter_factory=None, flipper_factory=None):
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
                     flipper_factory=flipper_factory)
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


def test_predict_on_an_unknown_lesson_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/predict?lesson_id=999", data={"prediction": "a"})
    assert resp.status_code == 404


def test_opening_an_unknown_lesson_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/lesson/999")
    assert resp.status_code == 404


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
    assert body["freq_hz"] == 929_600_000
    page = body["pages"][0]
    assert page["address"] == 1234567
    assert page["kind"] == "alpha"
    assert page["text"] == "CALL EXT 2210 URGENT"


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
    # rtl_ais present (injected which) so this pins the no-radio refusal, not a missing-tool one.
    with _client(tmp_path,
                 ais_factory=lambda: AisDecoder(
                     radio_present=lambda: False,
                     which=lambda name: "/usr/bin/" + name)) as client:
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


def test_rayhunter_default_monitor_also_refuses_without_an_orbic(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/rayhunter")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


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
    assert body["device"]["name"] == "Lle0zik"


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


# --- exam trainer: one study card, commit-before-reveal (like the predict gate) ---------

# A known-good pool question to drive the answer/review endpoints with, independent of which card
# /exam happens to surface first (that ordering is the store's business, not these tests').
_Q = exam.load_pool().get("T1A01")


def test_exam_serves_a_study_card_with_tap_choices_and_no_answer(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/exam")
    assert resp.status_code == 200
    # It surfaces a real pool question (its id is in the eyebrow) with four tap choices.
    served = re.search(r"EXAM &middot; (T\w+)", resp.text).group(1)
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

    with _client(tmp_path, ppm_measure=fake_measure,
                 sweeper_factory=lambda: _RecordingSweeper(_rows())) as client:
        client.get("/api/spectrum")  # start the sweep so there is something to stand down
        client_sweeper["running"] = True
        client.post("/api/ppm/measure?hz=98100000")
    assert seen.get("stopped_before_measure") is True
