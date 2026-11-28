# ABOUTME: Replays through the console's public routes using a SigMF pair and decoder fakes.
# ABOUTME: Checks handoff, shutdown, trial and separation of replay and live credit.

import re
import signal
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager

import pytest
from fastapi.testclient import TestClient

from sigint101 import app as console
from sigint101 import replay
from sigint101.decode import Decoder
from sigint101.events import read_events
from sigint101.spectrum import Sweeper
from sigint101.stamps import stamps_from_log
from sigint101.trial import create_trial_app
from tests.child_fakes import Child
from tests.test_app import _absent, _present
from tests.test_replay import DIGEST, LINE, _pair, _until
from tests.test_stamps import _hear, _ism
from tests.test_updater_validation import _make_managed_generation

START, STOP, STATE = (f"/api/replay/ism{suffix}" for suffix in ("/start", "/stop", ""))
INDEX = "/api/replay/recordings"
KEYS = {"mode", "replay", "instrument", "state", "recording", "digest", "frequency_hz",
        "sample_rate", "command", "input", "decodes", "reason", "retry_in"}


def _answer(response, status):
    assert response.status_code == status, response.text
    body = response.json()
    assert KEYS <= body.keys()
    assert body["mode"] == "replay" and body["replay"] is True
    return body


@contextmanager
def _client(tmp_path, *, no_radio=True, spawn=None, which=lambda name: name, live_child=None):
    data, recordings = tmp_path / "data", tmp_path / "recordings"
    _pair(recordings)
    live, played = [], []

    def live_spawn(*args):
        live.append(live_child if live_child is not None else Child(("rtl_433", "-d", "0")))
        return live[-1]

    def replay_spawn(argv, **kwargs):
        child = spawn(argv, **kwargs) if spawn else Child(argv, said=LINE)
        played.append(child)
        return child

    def no_survey():
        pytest.fail("replay surveyed or claimed the SDR")

    def sweeper_factory():
        sweeper = Sweeper(radio_present=lambda: False)
        if no_radio:
            sweeper.start = sweeper.release = no_survey
        return sweeper

    app = console.create_app(
        data_dir=data, recordings_dir=recordings, probe=_absent if no_radio else _present,
        list_devices=no_survey if no_radio else lambda: (),
        decoder_factory=lambda: Decoder(spawn=live_spawn, which=lambda name: name,
                                        radio_present=lambda: not no_radio),
        sweeper_factory=sweeper_factory,
        replay_factory=lambda **deps: replay.ReplayDecoder(spawn=replay_spawn, which=which, **deps),
    )
    with TestClient(app) as client:
        yield client, live, played


def test_recording_index_and_full_replay_without_a_radio(tmp_path):
    with _client(tmp_path) as (client, live, played):
        _pair(tmp_path / "recordings", "bad", meta={})
        index = client.get(INDEX)
        assert index.status_code == 200
        index = index.json()
        [recording] = index["recordings"]
        assert recording["stem"] == "capture" and recording["instrument"] == "ism"
        assert recording["frequency_hz"] == 433_920_000 and recording["sample_rate"] == 250_000
        assert set(recording) == {"stem", "instrument", "frequency_hz", "sample_rate", "datetime",
                                  "hw", "license", "source", "bytes"}
        assert [item["stem"] for item in index["refused"]] == ["bad"]
        assert _answer(client.get(STATE), 200)["state"] == "idle"
        assert _answer(client.post(START, json={"recording": "capture"}), 200)["digest"] == DIGEST
        _until(lambda: _answer(client.get(STATE), 200)["decodes"])
        assert not live and len(played) == 1
        assert _answer(client.post(STOP), 200)["state"] == "stopped"
        assert _answer(client.post(STOP), 200)["state"] == "stopped"


@pytest.mark.parametrize(("method", "path", "stem", "status"), [
    ("POST", START, "missing", 404), ("POST", START, "bad", 400),
    ("POST", "/api/replay/aprs/start", "capture", 404),
    ("POST", "/api/replay/aprs/stop", None, 404),
    ("GET", "/api/replay/aprs", None, 404),
])
def test_replay_route_refuses_with_a_labelled_state(tmp_path, method, path, stem, status):
    with _client(tmp_path) as (client, live, played):
        _pair(tmp_path / "recordings", "bad", meta={})
        body = _answer(client.request(method, path, **(
            {"json": {"recording": stem}} if stem else {})), status)
        assert body["reason"]
        assert not played and not live


def test_replay_busy_and_missing_tool_refusals(tmp_path):
    with _client(tmp_path) as (client, _, played):
        _answer(client.post(START, json={"recording": "capture"}), 200)
        busy = client.post(START, json={"recording": "capture"})
        assert _answer(busy, 409)["state"] == "replaying"
        assert len(played) == 1
    with _client(tmp_path, which=lambda _: None) as (client, _, played):
        body = _answer(client.post(START, json={"recording": "capture"}), 503)
        assert body["cause"] == "missing_tool" and "rtl_433" in body["reason"]
        assert not played


def test_start_stands_down_live_and_a_queued_poll_cannot_restart_it(tmp_path, monkeypatch):
    entered, unblock, queued = threading.Event(), threading.Event(), threading.Event()

    def spawn(argv, **_):
        entered.set()
        assert unblock.wait(5)
        return Child(argv, said=LINE)

    with _client(tmp_path, no_radio=False, spawn=spawn) as (client, live, played):
        assert client.get("/api/decode").status_code == 200
        initial = live[0]
        arbiter = client.app.state.radio_arbiter
        reserve = arbiter.reserve

        def observed(target, *args):
            if target == "decoder":
                queued.set()
            return reserve(target, *args)

        monkeypatch.setattr(arbiter, "reserve", observed)
        with ThreadPoolExecutor(2) as pool:
            start = pool.submit(client.post, START, json={"recording": "capture"})
            try:
                assert entered.wait(3), start.result(timeout=3).text
                assert initial.sent == ["TERM"]
                poll = pool.submit(client.get, "/api/decode")
                # The poll may refuse before queueing once a run is visible.
                assert queued.wait(1) or poll.done()
            finally:
                unblock.set()
            _answer(start.result(timeout=5), 200)
            refusal = poll.result(timeout=5)
        assert refusal.status_code == 503
        assert refusal.json()["cause"] == "replaying" and refusal.json()["decodes"] == []
        assert len(live) == len(played) == 1
        _answer(client.post(STOP), 200)
        assert client.get("/api/decode").status_code == 200
        assert len(live) == 2


def test_console_shutdown_reaps_the_replay_child(tmp_path):
    with _client(tmp_path) as (client, _, played):
        state = client.app.state
        _answer(client.post(START, json={"recording": "capture"}), 200)
        assert played[0].poll() is None
    assert played[0].poll() is not None
    assert state.shutdown_errors == []


def test_replay_refuses_to_start_until_live_decoder_exit_is_confirmed(tmp_path):
    child = Child(("rtl_433", "-d", "0"), unkillable=True)
    with _client(tmp_path, no_radio=False, live_child=child) as (client, live, played):
        assert client.get("/api/decode").status_code == 200
        assert live == [child]
        try:
            refused = _answer(client.post(START, json={"recording": "capture"}), 503)
            assert "rtl_433 survived kill" in refused["reason"]
            assert child.sent == ["TERM", "KILL"] and child.poll() is None
            assert not played
        finally:
            child.code = -signal.SIGKILL
        _answer(client.post(START, json={"recording": "capture"}), 200)
        assert len(played) == 1


def test_trial_does_not_construct_or_start_replay(tmp_path, monkeypatch):
    generation = _make_managed_generation(tmp_path, "gen1")
    original = {path.name: path.read_bytes() for path in generation.iterdir() if path.is_file()}

    def unexpected(**_):
        pytest.fail("the read-only trial constructed a replay")

    monkeypatch.setattr(console, "ReplayDecoder", unexpected)
    trial = create_trial_app(pack_dir=console.PACK_DIR, data_dir=generation,
                             release_root=console.REPO_DIR, require_release_manifest=False,
                             probe=_absent)
    with TestClient(trial) as client:
        assert not hasattr(trial.state, "replay")
        assert client.post(START, json={"recording": "capture"}).status_code == 404
        assert client.get(STATE).status_code == 404
    assert {p.name: p.read_bytes() for p in generation.iterdir() if p.is_file()} == original


def test_replay_evidence_is_separate_from_live_and_survives_restart(tmp_path):
    with _client(tmp_path) as (client, _, _):
        _answer(client.post(START, json={"recording": "capture"}), 200)
        _until(lambda: _ism(client)["replayed"])
        row = _ism(client)
        assert row["count"] == 0 and row["provenance"] is None and row["digest"] is None
        assert row["replayed"]["count"] == 1 and row["replayed"]["digest"] == DIGEST
        html = client.get("/logbook").text
        assert "Not heard live yet" in html and "Replayed recordings/capture" in html
        assert not client.get("/api/receptions").json()["receptions"]
        _answer(client.post(STOP), 200)
    data = tmp_path / "data"
    assert not list(stamps_from_log(read_events(data / "events.jsonl")))
    with closing(sqlite3.connect(data / "progress.db")) as db:
        for table in ("stamps", "stamp_events"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    with _client(tmp_path) as (client, _, _):
        assert _ism(client) == row
        _hear(client)
        both = _ism(client)
        assert both["count"] == 1 and both["replayed"] == row["replayed"]
        html = client.get("/logbook").text.split('data-logbook-instrument="ism"', 1)[1]
        ism = html.split("</li>", 1)[0]
        assert ">STAMP<" in ism and ">REPLAY<" in ism


def test_empty_kit_shows_a_disabled_replay_key_on_both_pages(tmp_path):
    with TestClient(console.create_app(data_dir=tmp_path, probe=_absent,
                                       recordings_dir=tmp_path / "absent")) as client:
        assert client.get(INDEX).json() == {"recordings": [], "refused": []}
        for url in ("/lesson/10", "/operate/ism"):
            html = client.get(url).text
            keys = re.findall(r"<button\b[^>]*\bdata-replay-toggle disabled[^>]*>([^<]*)</button>",
                              html)
            assert keys and all(text.strip() == "No recording on this kit yet" for text in keys)
