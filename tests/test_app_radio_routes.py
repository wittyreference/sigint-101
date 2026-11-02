# ABOUTME: Route tests for the radio panels: a dead tool reads stopped, every route shows its argv.
# ABOUTME: Also the single claim lock, deduplicated refusal logging, and the ppm route's claim.

import shlex
import threading
import time

import pytest

from sigint101 import childproc
from sigint101.acars import AcarsDecoder
from sigint101.adsb import AdsbDecoder
from sigint101.ais import AisDecoder
from sigint101.aprs import AprsDecoder
from sigint101.apt import AptDecoder
from sigint101.decode import Decoder
from sigint101.dmr import DmrDecoder
from sigint101.events import log_event, read_events
from sigint101.flock import FlockDetector
from sigint101.hardware import RtlSdrDevice
from sigint101.lora import LoRaListener
from sigint101.pocsag import PocsagDecoder
from sigint101.ppm import PpmUnavailable
from sigint101.rds import RDSDecoder
from sigint101.sstv import SstvDecoder
from tests.child_fakes import USB_BUSY, Child
from tests.test_app import (
    _SDR_CONSUMERS,
    _aprs_stations,
    _client,
    _present,
    _reads,
    _recording_sdr_client,
    _rows,
    _StubAprs,
    _StubDecoder,
    _StubSweeper,
)


def _live():
    return {"radio_present": lambda: True, "which": lambda name: "/usr/bin/" + name}


# path, factory kwarg, build(spawn, tmp_path), the route's list key, its event prefix
ROUTES = [
    ("/api/decode", "decoder_factory", lambda s, t: Decoder(spawn=s, **_live()),
     "decodes", "decode"),
    ("/api/rds", "rds_factory", lambda s, t: RDSDecoder(spawn=s, **_live()), "station", "rds"),
    ("/api/adsb", "adsb_factory", lambda s, t: AdsbDecoder(spawn=s, **_live()),
     "aircraft", "adsb"),
    ("/api/aprs", "aprs_factory", lambda s, t: AprsDecoder(spawn=s, **_live()),
     "stations", "aprs"),
    ("/api/pocsag", "pocsag_factory", lambda s, t: PocsagDecoder(spawn=s, **_live()),
     "pages", "pocsag"),
    ("/api/acars", "acars_factory", lambda s, t: AcarsDecoder(spawn=s, **_live()),
     "messages", "acars"),
    ("/api/ais", "ais_factory", lambda s, t: AisDecoder(spawn=s, **_live()), "vessels", "ais"),
    ("/api/dmr", "dmr_factory", lambda s, t: DmrDecoder(spawn=s, **_live()), "calls", "dmr"),
    ("/api/apt", "apt_factory",
     lambda s, t: AptDecoder(spawn=s, output_dir=t / "apt", **_live()), "images", "apt"),
    ("/api/sstv", "sstv_factory",
     lambda s, t: SstvDecoder(spawn=s, output_dir=t / "sstv", data_dir=t, **_live()),
     "images", "sstv"),
    ("/api/flock", "flock_factory", lambda s, t: FlockDetector(spawn=s, **_live()),
     "detections", "flock"),
]
ROUTE_IDS = [r[0] for r in ROUTES]


@pytest.fixture(autouse=True)
def _quick_stops(monkeypatch):
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0.01)


def _kinds(tmp_path):
    return [e["kind"] for e in read_events(tmp_path / "events.jsonl")]


# --- R12: the command strip's source, per route ------------------------------------------------


@pytest.mark.parametrize("path,factory,build,key,kind", ROUTES, ids=ROUTE_IDS)
def test_a_running_route_names_the_exact_command(tmp_path, path, factory, build, key, kind):
    argv = ["rtl_fm", "-M", "fm", "-f", "144390000", "-s", "22050"]
    children = []

    def spawn(*_):
        children.append(Child(argv))
        return children[-1]

    with _client(tmp_path, probe=_present, **{factory: lambda: build(spawn, tmp_path)}) as client:
        body = client.get(path).json()
    assert body["available"] is True
    # Read off the running process, not rebuilt from settings: the line a learner can type.
    assert body["command"] == shlex.join(argv)


@pytest.mark.parametrize("path,factory,build,key,kind", ROUTES, ids=ROUTE_IDS)
def test_a_fresh_tool_reads_starting_until_it_settles(tmp_path, path, factory, build, key, kind):
    # A retry on a busy dongle spawns a tool that dies a moment later; the poll in between must
    # not call it live.
    now = [1000.0]
    dec = build(lambda *_: Child(["rtl_433", "-f", "433920000"]), tmp_path)
    dec.watch.clock = lambda: now[0]
    with _client(tmp_path, probe=_present, **{factory: lambda: dec}) as client:
        assert client.get(path).json()["state"] == "starting"
        now[0] += childproc.SETTLE_S
        assert client.get(path).json()["state"] == "running"


@pytest.mark.parametrize("path,factory,build,key,kind", ROUTES, ids=ROUTE_IDS)
def test_a_refused_route_names_no_command(tmp_path, path, factory, build, key, kind):
    def missing(name):
        return None

    def build_without_tools():
        dec = build(lambda *_: Child(["x"]), tmp_path)
        dec._which = missing
        return dec

    with _client(tmp_path, probe=_present, **{factory: build_without_tools}) as client:
        resp = client.get(path)
    assert resp.status_code == 503
    assert resp.json()["command"] is None


def test_spectrum_names_the_sweep_command_while_it_runs(tmp_path):
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        body = client.get("/api/spectrum").json()
    assert body["command"] == "rtl_power -f 88000000:108000000:125000"


@pytest.mark.parametrize("path,body_key", [("/api/lora", "packets"),
                                           ("/api/rayhunter", "warnings")])
def test_routes_that_run_no_process_say_so(tmp_path, path, body_key):
    def refuse(host, port):
        raise OSError("connection refused")

    def unreachable(url, timeout):
        raise OSError("no route to host")

    from sigint101.rayhunter import RayhunterMonitor
    with _client(tmp_path, lora_factory=lambda: LoRaListener(connect=refuse),
                 rayhunter_factory=lambda: RayhunterMonitor(fetch=unreachable)) as client:
        body = client.get(path).json()
    assert body["command"] is None
    assert body[body_key] == []


# --- T-01: a dead tool reads stopped, is not respawned every poll, and does not spam events ---


@pytest.mark.parametrize("path,factory,build,key,kind", ROUTES, ids=ROUTE_IDS)
def test_a_dead_tool_reads_stopped_not_live(tmp_path, path, factory, build, key, kind):
    spawned = []

    def spawn(*_):
        spawned.append(Child(["rtl_fm", "-f", "1"], code=1, said=USB_BUSY))
        return spawned[-1]

    with _client(tmp_path, probe=_present, **{factory: lambda: build(spawn, tmp_path)}) as client:
        client.get(path)                          # launches it; it dies at once
        polls = [client.get(path) for _ in range(6)]
    for resp in polls:
        assert resp.status_code == 503
        body = resp.json()
        assert body["available"] is False
        assert body["state"] == "stopped"
        assert "exit status 1" in body["reason"]
        assert "usb_claim_interface error -6" in body["reason"]
        assert body["retry_in"] > 0
        assert body["command"] is None
        assert not body[key]                      # nothing served as if it were live
    assert len(spawned) == 1                      # backed off, not relaunched on every poll
    kinds = _kinds(tmp_path)
    assert kinds.count(f"{kind}_start") == 1
    assert kinds.count(f"{kind}_blocked") == 1    # one line for the death, not one per poll


def test_a_missing_tool_is_logged_once_not_every_poll(tmp_path):
    # T-15: a missing tool used to add a *_blocked line on every poll, 14 a minute.
    with _client(tmp_path, probe=_present,
                 decoder_factory=lambda: Decoder(radio_present=lambda: True,
                                                 which=lambda name: None)) as client:
        for _ in range(10):
            assert client.get("/api/decode").status_code == 503
    assert _kinds(tmp_path).count("decode_blocked") == 1


def test_a_refusal_is_logged_again_after_a_start(tmp_path):
    # The dedupe is per episode: after the tool ran again, a new refusal is news.
    which = {"ok": False}
    dec = Decoder(radio_present=lambda: True, spawn=lambda cmd: Child(cmd),
                  which=lambda name: "/usr/bin/x" if which["ok"] else None)
    with _client(tmp_path, probe=_present, decoder_factory=lambda: dec) as client:
        client.get("/api/decode")
        which["ok"] = True
        client.get("/api/decode")
        dec.stop()
        which["ok"] = False
        client.get("/api/decode")
    assert _kinds(tmp_path).count("decode_blocked") == 2


# --- T-17: handing the radio over and starting the new tenant is one step ----------------------


def test_a_claim_cannot_land_inside_another_routes_start(tmp_path):
    log = []

    class _SlowDecoder(_StubDecoder):
        def start(self):
            log.append("decode start begins")
            time.sleep(0.3)
            super().start()
            log.append("decode start ends")

        def release(self):
            log.append("decode released")
            super().release()

    with _client(tmp_path, probe=_present, sweeper_factory=lambda: _StubSweeper(_rows()),
                 decoder_factory=lambda: _SlowDecoder(_reads()),
                 aprs_factory=lambda: _StubAprs(_aprs_stations())) as client:
        first = threading.Thread(target=lambda: client.get("/api/decode"))
        first.start()
        time.sleep(0.1)                           # the decoder is mid-start
        client.get("/api/aprs")                   # a second poller wants the radio
        first.join()
    assert log.index("decode released") > log.index("decode start ends")


# --- T-07: ppm claims the radio like any tenant, by index, and refuses cleanly ----------------


def test_ppm_measure_stands_every_sdr_consumer_down(tmp_path):
    client, calls = _recording_sdr_client(tmp_path)
    with client:
        client.app.state.ppm_measure = lambda **kw: {"hz": kw["hz"], "ppm": 1.0, "reason": ""}
        assert client.post("/api/ppm/measure?hz=98100000").status_code == 200
    assert set(calls["release"]) == set(_SDR_CONSUMERS)


def test_ppm_measure_holds_the_radio_until_it_finishes(tmp_path):
    # The waterfall polls every 0.9 s beside the ppm panel; mid-measure it must not take the
    # dongle back (the audit saw rtl_power respawn twice during one measurement).
    log = []

    class _Sweeper(_StubSweeper):
        def start(self):
            log.append("sweep start")
            super().start()

    def slow_measure(**kw):
        log.append("measure begins")
        time.sleep(0.4)
        log.append("measure ends")
        return {"hz": kw["hz"], "ppm": 2.0, "reason": ""}

    with _client(tmp_path, probe=_present, ppm_measure=slow_measure,
                 sweeper_factory=lambda: _Sweeper(_rows())) as client:
        measuring = threading.Thread(target=lambda: client.post("/api/ppm/measure?hz=98100000"))
        measuring.start()
        time.sleep(0.1)
        client.get("/api/spectrum")               # the waterfall's poll arrives mid-measure
        measuring.join()
    assert log.index("sweep start") > log.index("measure ends")


def test_ppm_measure_without_rtl_fm_is_a_reasoned_503_not_a_500(tmp_path):
    def no_rtl_fm(**kw):
        raise PpmUnavailable("rtl_fm is not installed, so SIGINT 101 cannot measure the pilot")

    with _client(tmp_path, ppm_measure=no_rtl_fm,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.post("/api/ppm/measure?hz=98100000")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["ppm"] is None
    assert "rtl_fm is not installed" in body["reason"]


def test_ppm_measure_opens_the_pinned_radio_by_index_not_serial(tmp_path, monkeypatch):
    # rtl_fm reads "-d 00000001" as index 1; the serial has to be looked up first.
    monkeypatch.setenv("SIGINT101_RTLSDR_SERIAL", "00000001")
    seen = {}

    def measure(**kw):
        seen.update(kw)
        return {"hz": kw["hz"], "ppm": 3.0, "reason": ""}

    from fastapi.testclient import TestClient

    from sigint101.app import create_app
    from tests.test_app import FIXTURE_PACKS, _absent
    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path, probe=_absent, ppm_measure=measure,
                     sweeper_factory=lambda: _StubSweeper(_rows()),
                     list_devices=lambda: (RtlSdrDevice(0, "1004"), RtlSdrDevice(1, "00000001")))
    with TestClient(app) as client:
        assert client.post("/api/ppm/measure?hz=98100000").status_code == 200
    assert seen["device"] == 1


def test_ppm_measure_refuses_a_pinned_radio_that_is_not_there(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGINT101_RTLSDR_SERIAL", "00000001")
    from fastapi.testclient import TestClient

    from sigint101.app import create_app
    from tests.test_app import FIXTURE_PACKS, _absent
    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path, probe=_absent,
                     ppm_measure=lambda **kw: pytest.fail("measured the wrong radio"),
                     sweeper_factory=lambda: _StubSweeper(_rows()),
                     list_devices=lambda: (RtlSdrDevice(0, "1004"),))
    with TestClient(app) as client:
        resp = client.post("/api/ppm/measure?hz=98100000")
    assert resp.status_code == 503
    assert "00000001" in resp.json()["reason"]


# --- T-15: the provenance poll follows the event log instead of re-reading it -----------------


def test_provenance_poll_does_not_reread_the_event_log(tmp_path):
    events = tmp_path / "events.jsonl"
    log_event(events, "baseline_sample", {"ref_db": -20.0})
    log_event(events, "baseline_sample", {"ref_db": -21.0})
    with _client(tmp_path) as client:
        assert client.get("/api/provenance").json()["baseline"]["n_samples"] == 2
        # Scribble over what was already read. A poll that re-read the whole file would lose it.
        size = events.stat().st_size
        events.write_bytes(b"x" * (size - 1) + b"\n")
        assert client.get("/api/provenance").json()["baseline"]["n_samples"] == 2
        # ...while what is appended after is still picked up.
        log_event(events, "baseline_sample", {"ref_db": -22.0})
        assert client.get("/api/provenance").json()["baseline"]["n_samples"] == 3
