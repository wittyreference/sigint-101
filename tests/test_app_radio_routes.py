# ABOUTME: Route tests for the radio panels: a dead tool reads stopped, every route shows its argv.
# ABOUTME: Also the single claim lock, deduplicated refusal logging, and the ppm route's claim.

import json
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

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
from sigint101.hardware import RtlSdrDevice, probe_rtlsdr
from sigint101.lora import LoRaListener
from sigint101.pocsag import PocsagDecoder
from sigint101.ppm import PpmUnavailable
from sigint101.provenance import REFERENCE_HZ
from sigint101.rds import RDSDecoder
from sigint101.spectrum import DEFAULT_BIN, DEFAULT_HIGH, DEFAULT_LOW
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
from tests.test_hardware import _lsusb, sysfs_tree

# What a tool says when the kernel has deauthorized the radio it tries to claim.
CLAIM_FAILS = "usb_claim_interface error -5\nFailed to open rtlsdr device #0.\n"


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
        assert body["state"] == body["cause"] == "stopped"
        assert "exit status 1" in body["reason"]
        assert "usb_claim_interface error -6" in body["reason"]
        assert body["retry_in"] > 0
        assert body["command"] is None
        assert not body[key]                      # nothing served as if it were live
    assert len(spawned) == 1                      # backed off, not relaunched on every poll
    kinds = _kinds(tmp_path)
    assert kinds.count(f"{kind}_start") == 1
    assert kinds.count(f"{kind}_blocked") == 1    # one line for the death, not one per poll


# --- C30: after a death, only the death reads stopped; any other refusal reads as what it is ---
# WHY: the console keeps a tool's death on record until its next spawn, and it badged every refusal
# in that time STOPPED with the retry countdown. The radio check sits before the spawn, so a radio
# unplugged after a death read STOPPED "· retrying now" for as long as it stayed away, though
# nothing was retrying; so did a refusal from a check after the back-off, such as a tool that
# cannot run. Only the death's own reason is STOPPED now. The record and its back-off still stand:
# a radio that comes back inside the back-off waits it out, STOPPED with its countdown, as before.

STATIC = Path(__file__).resolve().parent.parent / "sigint101" / "static"

# The refusals a decoder gives when its radio is gone: an RTL-SDR's, and Flock's monitor interface.
NO_RADIO = ("no RTL-SDR on the USB bus", "no monitor interface")


class _Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


def _panel_shows(kind, bodies):
    """Each refused body as the route's panel shows it: decoder-panel.js's downState, in node.

    Each panel passes the API body through unchanged. The helper decides the displayed label
    from the cause, so this checks the complete API-to-panel contract in both languages.
    """
    assert "DecoderPanel.showDown(panel, badge, empty, body)" in (
        STATIC / f"{kind}.js").read_text(encoding="utf-8")
    node = shutil.which("node")
    assert node, "node is not on PATH, so the panel's own rendering cannot be checked"
    script = ("const p = require(process.argv[1]);"
              "const bodies = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "process.stdout.write(JSON.stringify(bodies.map(b => p.downState(b))));")
    result = subprocess.run([node, "-e", script, str(STATIC / "decoder-panel.js")],
                            input=json.dumps(bodies), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _dying_then_running(spawned, cannot_run=None):
    """A spawn whose first tool dies at once on a busy dongle and whose later ones keep running.

    While `cannot_run[0]` is true, the spawn fails as a missing or broken binary does.
    """
    def spawn(*_):
        if cannot_run and cannot_run[0]:
            raise OSError(8, "Exec format error")
        spawned.append(Child(["tool"], code=1, said=USB_BUSY) if not spawned else Child(["tool"]))
        return spawned[-1]
    return spawn


def _assert_stopped(body, *, counting):
    assert body["available"] is False, body
    assert body["state"] == "stopped", body
    assert "exit status 1" in body["reason"], body
    assert (body["retry_in"] > 0) is counting, body


def _assert_refused_not_stopped(body, words):
    assert body["available"] is False, body
    assert any(word in body["reason"] for word in words), body
    assert "exit status" not in body["reason"], body
    assert "state" not in body and "retry_in" not in body, body


@pytest.mark.parametrize("path,factory,build,key,kind", ROUTES, ids=ROUTE_IDS)
def test_after_a_death_a_missing_radio_reads_missing_not_stopped(
        tmp_path, path, factory, build, key, kind):
    spawned = []
    dec = build(_dying_then_running(spawned), tmp_path)
    clock = _Clock()
    dec.watch.clock = clock
    present = [True]
    dec._radio_present = lambda: present[0]
    with _client(tmp_path, probe=_present, **{factory: lambda: dec}) as client:
        assert client.get(path).json()["available"] is True    # launched; it dies at once
        dec.drain()                                             # its end is read before a poll
        died = client.get(path).json()
        present[0] = False                                      # the radio is unplugged
        gone = [client.get(path).json()]                        # inside the back-off
        clock.now += 10 * childproc.MAX_RETRY_S                 # and long after it
        gone += [client.get(path).json() for _ in range(3)]
        present[0] = True                                       # plugged back in
        back = client.get(path).json()
        dec.stop()
    _assert_stopped(died, counting=True)
    for body in gone:
        _assert_refused_not_stopped(body, NO_RADIO)
    assert back["available"] is True and back["state"] == "starting", back
    assert len(spawned) == 2                                    # it started normally, once
    assert dec.watch.stopped is False

    shown = _panel_shows(kind, [died, *gone])
    assert shown[0]["badge"] == "STOPPED"
    assert shown[0]["reason"] == died["reason"] + " · retrying in 2 s"
    for down, body in zip(shown[1:], gone):
        assert down["badge"] == ("UNAVAILABLE" if kind == "flock" else "NO RADIO"), down
        assert down["reason"] == body["reason"], down            # no countdown, no "retrying"


@pytest.mark.parametrize("path,factory,build,key,kind", ROUTES, ids=ROUTE_IDS)
def test_a_radio_back_inside_the_back_off_still_waits_it_out_stopped(
        tmp_path, path, factory, build, key, kind):
    # The missing radio's refusal must not end the death's record: the back-off still stands.
    spawned = []
    dec = build(_dying_then_running(spawned), tmp_path)
    clock = _Clock()
    dec.watch.clock = clock
    present = [True]
    dec._radio_present = lambda: present[0]
    with _client(tmp_path, probe=_present, **{factory: lambda: dec}) as client:
        client.get(path)                                        # launched; it dies at once
        dec.drain()
        died = client.get(path).json()
        present[0] = False
        client.get(path)
        present[0] = True
        clock.now += 1.0                                        # half the first back-off
        waiting = client.get(path).json()
        assert len(spawned) == 1                                # not relaunched early
        clock.now += 1.0
        back = client.get(path).json()
        dec.stop()
    _assert_stopped(died, counting=True)
    _assert_stopped(waiting, counting=True)
    assert waiting["reason"] == died["reason"]
    assert waiting["retry_in"] == 1.0
    assert _panel_shows(kind, [waiting])[0] == {
        "badge": "STOPPED", "cls": "decoder__status decoder__status--stopped",
        "reason": died["reason"] + " · retrying in 1 s"}
    assert back["available"] is True and len(spawned) == 2


@pytest.mark.parametrize("path,factory,build,key,kind", ROUTES, ids=ROUTE_IDS)
def test_after_a_death_a_tool_that_cannot_run_reads_so_not_stopped(
        tmp_path, path, factory, build, key, kind):
    # The spawn is the last check, after the back-off: once the back-off has run out, a tool that
    # cannot run refuses every poll, and no spawn comes to end the death's record.
    spawned, cannot_run = [], [False]
    dec = build(_dying_then_running(spawned, cannot_run), tmp_path)
    clock = _Clock()
    dec.watch.clock = clock
    with _client(tmp_path, probe=_present, **{factory: lambda: dec}) as client:
        client.get(path)                                        # launched; it dies at once
        dec.drain()
        died = client.get(path).json()
        cannot_run[0] = True
        clock.now += 10 * childproc.MAX_RETRY_S
        refused = [client.get(path).json() for _ in range(3)]
        cannot_run[0] = False
        back = client.get(path).json()
        dec.stop()
    _assert_stopped(died, counting=True)
    for body in refused:
        _assert_refused_not_stopped(body, ["cannot run"])
        assert "Exec format error" in body["reason"]
    for down in _panel_shows(kind, refused):
        assert down["badge"] == "UNAVAILABLE" and "retrying" not in down["reason"], down
    assert back["available"] is True and back["state"] == "starting", back
    assert len(spawned) == 2


# APT has no reader to see its capture end between polls: its death is found by the next poll and
# so is always told inside its back-off. Every other route can be told it late.
TOLD_LATE = [route for route in ROUTES if route[0] != "/api/apt"]


@pytest.mark.parametrize("path,factory,build,key,kind", TOLD_LATE,
                         ids=[route[0] for route in TOLD_LATE])
def test_a_death_first_reported_after_its_back_off_reads_stopped_retrying_now(
        tmp_path, path, factory, build, key, kind):
    # Nobody polled while the back-off ran, so the death is told late: it is still the death, and
    # the very next poll does try again, so "retrying now" is true.
    spawned = []
    dec = build(_dying_then_running(spawned), tmp_path)
    clock = _Clock()
    dec.watch.clock = clock
    with _client(tmp_path, probe=_present, **{factory: lambda: dec}) as client:
        client.get(path)                                        # launched; it dies at once
        dec.drain()
        clock.now += 10 * childproc.MAX_RETRY_S
        late = client.get(path).json()
        back = client.get(path).json()
        dec.stop()
    _assert_stopped(late, counting=False)
    assert late["retry_in"] == 0
    assert _panel_shows(kind, [late])[0]["reason"] == late["reason"] + " · retrying now"
    assert back["available"] is True and len(spawned) == 2


def test_a_missing_tool_is_logged_once_not_every_poll(tmp_path):
    # T-15: a missing tool used to add a *_blocked line on every poll, 14 a minute.
    with _client(tmp_path, probe=_present,
                 decoder_factory=lambda: Decoder(radio_present=lambda: True,
                                                 which=lambda name: None)) as client:
        for _ in range(10):
            assert client.get("/api/decode").status_code == 503
    assert _kinds(tmp_path).count("decode_blocked") == 1


@pytest.mark.parametrize("manifest,report,starts", [
    ((200, b'{"entries": [], "current_entry": {"name": "1730300000"}}'), (500, b"error"), 1),
    ((200, b"<html><title>Router login</title></html>"), (404, b""), 0),
], ids=["report-unreadable", "not-rayhunter"])
def test_an_unreadable_rayhunter_is_logged_once_not_every_poll(tmp_path, manifest, report, starts):
    # An Orbic whose answers cannot be read refuses every poll; like a missing tool, that is one
    # line, not a start and a refusal per poll.
    from sigint101.rayhunter import RayhunterMonitor

    def fetch(url, timeout):
        return manifest if url.endswith("/api/qmdl-manifest") else report

    with _client(tmp_path, rayhunter_factory=lambda: RayhunterMonitor(fetch=fetch)) as client:
        for _ in range(5):
            assert client.get("/api/rayhunter").status_code == 503
    kinds = _kinds(tmp_path)
    assert kinds.count("rayhunter_blocked") == 1
    assert kinds.count("rayhunter_start") == starts


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
    # Taken under the set-up a console with no radio reports, so every sample counts.
    setup = {"serial": None, "gain": None, "ref_hz": REFERENCE_HZ, "bin_size": DEFAULT_BIN,
             "freq_low": DEFAULT_LOW, "freq_high": DEFAULT_HIGH}
    log_event(events, "baseline_sample", {"ref_db": -20.0, **setup})
    log_event(events, "baseline_sample", {"ref_db": -21.0, **setup})
    with _client(tmp_path) as client:
        assert client.get("/api/provenance").json()["baseline"]["n_samples"] == 2
        # Scribble over what was already read. A poll that re-read the whole file would lose it.
        size = events.stat().st_size
        events.write_bytes(b"x" * (size - 1) + b"\n")
        assert client.get("/api/provenance").json()["baseline"]["n_samples"] == 2
        # ...while what is appended after is still picked up.
        log_event(events, "baseline_sample", {"ref_db": -22.0, **setup})
        assert client.get("/api/provenance").json()["baseline"]["n_samples"] == 3


# --- A47: an RTL-SDR decoder's empty-bus refusal never names a waterfall ----------------------

SDR_DECODER_ROUTES = [r for r in ROUTES if r[0] != "/api/flock"]
SDR_DECODER_IDS = [r[0] for r in SDR_DECODER_ROUTES]


@pytest.mark.parametrize("path,factory,build,key,kind", SDR_DECODER_ROUTES, ids=SDR_DECODER_IDS)
def test_decoder_empty_bus_refusal_names_no_waterfall_and_fits_64_chars(
        tmp_path, path, factory, build, key, kind):
    def build_empty_bus():
        dec = build(lambda *_: Child(["tool"]), tmp_path)
        dec._list_devices = lambda: ()
        return dec

    with _client(tmp_path, probe=_present, **{factory: build_empty_bus}) as client:
        resp = client.get(path)
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    reason = body["reason"]
    assert "no RTL-SDR on the USB bus" in reason
    assert "SIGINT 101 will not fake" in reason
    assert "waterfall" not in reason.lower()
    assert len(reason) <= 64

    shown = _panel_shows(kind, [body])[0]
    assert body["cause"] == "no_radio"
    assert shown["badge"] == "NO RADIO"
    assert shown["reason"] == reason


# --- C50: a radio the kernel deauthorized reads "No radio", not a tool's death -----------------

@pytest.mark.parametrize("path,factory,build,key,kind", SDR_DECODER_ROUTES, ids=SDR_DECODER_IDS)
def test_a_deauthorized_radio_lsusb_still_lists_gives_no_radio(
        tmp_path, path, factory, build, key, kind):
    # R19 on the reference Recon: lsusb lists the built-in RTL2832U, sysfs has it authorized=0,
    # and the tool that tries to open it dies with usb_claim_interface error -5.
    root = sysfs_tree(tmp_path / "usb", ("1-1.2", "0bda:2838", "0"))

    def probe():
        return probe_rtlsdr(which=lambda name: "/usr/bin/" + name, run=_lsusb(),
                            sysfs_root=root)

    def build_on_a_deauthorized_radio():
        dec = build(lambda *_: Child(["tool"], code=2, said=CLAIM_FAILS), tmp_path)
        dec._radio_present = lambda: probe().present  # as create_app wires every receiver
        return dec

    with _client(tmp_path, probe=probe, **{factory: build_on_a_deauthorized_radio}) as client:
        body = client.get(path).json()
    assert body["available"] is False
    assert body["reason"].startswith("no RTL-SDR on the USB bus")
    assert body["cause"] == "no_radio"
    assert _panel_shows(kind, [body])[0]["badge"] == "NO RADIO"


@pytest.mark.parametrize(("reason", "cause", "next_step"), [
    ("no RTL-SDR on the USB bus — SIGINT 101 will not fake a waterfall", "no_radio",
     "Plug in the RTL-SDR."),
    ("cannot survey the bus: rtl_power is not installed, so the USB bus is unknown", "missing_tool",
     "Run deploy/provision.sh"),
    ("cannot run rtl_power: Exec format error", "unavailable", None),
    ("the sweep stopped (rtl_power status 1)", "stopped", None),
])
def test_spectrum_reports_the_refusal_cause(tmp_path, reason, cause, next_step):
    from sigint101.spectrum import SpectrumUnavailable

    class RefusingSweep(_StubSweeper):
        def start(self):
            raise SpectrumUnavailable(reason)

        def retry_in(self):
            return 1.25

    with _client(tmp_path, sweeper_factory=lambda: RefusingSweep([])) as client:
        response = client.get("/api/spectrum")
    assert response.status_code == 503
    body = response.json()
    assert body["cause"] == cause
    assert body["reason"] == reason
    assert body.get("next_step") == next_step
    if cause == "stopped":
        assert body["state"] == "stopped" and body["retry_in"] == 1.2
    else:
        assert "retry_in" not in body


@pytest.mark.parametrize("path,factory,build,key,kind", ROUTES, ids=ROUTE_IDS)
def test_missing_tool_refusals_name_the_correct_provision_command(
        tmp_path, path, factory, build, key, kind):
    dec = build(lambda *_: pytest.fail("must refuse before spawn"), tmp_path)
    dec._which = lambda tool: tool if kind == "sstv" and tool != "sstv" else None
    with _client(tmp_path, probe=_present, **{factory: lambda: dec}) as client:
        response = client.get(path)
    assert response.status_code == 503
    body = response.json()
    assert body["cause"] == "missing_tool"
    command = "Run deploy/provision.sh"
    if kind in ("apt", "dmr", "sstv"):
        command += " --with-optional"
    assert body["next_step"] == command
