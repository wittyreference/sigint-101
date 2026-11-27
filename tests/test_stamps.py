# ABOUTME: Exercises reception badge provenance, durability, and the logbook routes.
# ABOUTME: Fakes at the receiver boundary never open a radio or invent a product reception.

import ast
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest

from sigint101 import app as console
from sigint101 import events
from sigint101.hardware import RtlSdrDevice
from sigint101.operate import INSTRUMENTS
from sigint101.progress import Progress
from sigint101.sstv import SSTV, SstvDecoder
from tests.child_fakes import LateProc
from tests.test_operate_routes import _client
from tests.test_sstv import PNG_BYTES, _finish

KEYS = ("ism", "adsb", "aprs", "pager", "acars", "ais", "rds", "dmr", "apt", "sstv", "lora")
RADIO = {"kind": "rtl-sdr", "index": 0, "serial": None}
RX = {"model": "Test sensor", "readings": {"temperature_C": 20.0}}


def _event(key="ism", **payload):
    return {"kind": "reception", "ts": 123.25, "payload": {
        "instrument": key, "tool": "rtl_433", "radio": RADIO.copy(),
        "command": "rtl_433 -d 0", "freq_hz": 433_920_000, "item": {}, **payload,
    }}


def _mint(event):
    from sigint101.stamps import mint

    return mint(event)


@pytest.mark.parametrize("event", [
    None, [], {"kind": "reception", "payload": None}, {**_event(), "kind": "completed"},
    _event("flock"), _event("rayhunter"), _event("waterfall"), _event(["ism"]),
    _event(replay=True), _event(source="replay"), _event(source="sample"), _event(sample=True),
    _event(tool=" "), _event(command=None), _event(command=" "), _event(item=[]),
    _event(radio=None), _event(radio={"kind": "rtl-sdr", "index": None}),
    _event(radio={"kind": "rtl-sdr", "index": True}),
    _event(radio={"kind": "rtl-sdr", "index": -1}),
    _event(radio={"kind": "rtl-sdr", "index": 0, "serial": []}),
    _event(radio={"kind": "sample", "index": 0}),
    _event(freq_hz=True), _event(freq_hz=0), _event(freq_hz=[433_920_000]),
    _event("ais", freq_hz=[]), _event("ais", freq_hz=[161_975_000, False]),
    _event("lora", radio={"kind": "meshtasticd", "host": "None:None"}),
    _event("lora", radio={"kind": "meshtasticd", "host": "bridge:0"}),
    _event("lora", radio={"kind": "rtl-sdr", "index": 0}),
    *[{**_event(), "ts": invalid} for invalid in (None, True, 0, -1, float("nan"), float("inf"))],
], ids=[
    "none", "list", "no-payload", "not-reception", "flock", "rayhunter", "waterfall",
    "bad-instrument", "replay-marker", "replay-source", "sample-source", "sample-marker",
    "blank-tool", "missing-command", "blank-command", "bad-item", "missing-radio", "missing-index",
    "bool-index", "negative-index", "bad-serial", "sample-radio", "bool-hz", "zero-hz", "list-hz",
    "empty-ais", "bad-ais", "placeholder-lora", "zero-port", "wrong-radio",
    "no-time", "bool-time", "zero-time", "negative-time", "nan-time", "infinite-time",
])
def test_mint_refuses_non_live_or_incomplete_events(event):
    assert _mint(event) is None


def test_catalog_matches_receiver_keys_and_uses_instrument_specific_provenance():
    from sigint101.stamps import STAMP_INSTRUMENTS

    assert STAMP_INSTRUMENTS == tuple(inst.key for inst in INSTRUMENTS if inst.key in KEYS) == KEYS
    for key in KEYS:
        fields = {}
        if key == "ais":
            fields["freq_hz"] = [161_975_000, 162_025_000]
        if key == "lora":
            fields.update(tool="meshtasticd", radio={"kind": "meshtasticd", "host": "bridge:4403"},
                          command=None, freq_hz=None)
        assert _mint(_event(key, **fields))["instrument"] == key


def test_legacy_pager_digest_and_provenance_are_both_sanitized():
    event = _event("pager", tool="multimon-ng", summary="original words",
                   item={"kind": "Alpha", "address": 123, "text": "original words", "length": 5,
                         "encoding": "café"})
    stamp = _mint(event)
    expected = ('{"kind":"reception","payload":{"command":"rtl_433 -d 0","freq_hz":433920000,'
                '"instrument":"pager","item":{"address":123,"encoding":"café","kind":"Alpha",'
                '"length":5,"text":null,"text_withheld":true},"radio":{"index":0,'
                '"kind":"rtl-sdr","serial":null},'
                '"summary":"cap 123 · Alpha · 5 chars · text withheld",'
                '"tool":"multimon-ng"},"ts":123.25}')
    assert stamp["digest"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert stamp["provenance"] == json.loads(expected)["payload"]
    assert stamp["first_ts"] == event["ts"]
    event["payload"]["item"]["text"] = "different words"
    event["payload"]["summary"] = "different words"
    assert _mint(event) == stamp
    event["payload"]["radio"]["index"] = 2
    assert stamp["provenance"]["radio"]["index"] == 0


def _hear(client, **changes):
    kwargs = {"command": "rtl_433 -d 0", "freq_hz": 433_920_000, "radio": RADIO, **changes}
    client.app.state.decoder.on_reception(RX, **kwargs)


def _stamps(client):
    response = client.get("/api/stamps")
    assert response.status_code == 200
    return response.json()


def _ism(client):
    return next(row for row in _stamps(client)["stamps"] if row["instrument"] == "ism")


def test_first_reception_wins_concurrent_counts_and_survives_rotation(tmp_path, monkeypatch):
    with _client(tmp_path) as client:
        _hear(client)
        first = _ism(client)
        assert first["count"] == 1
        assert first["provenance"]["command"] == "rtl_433 -d 0"
        assert first["digest"] == _mint(events.read_events(tmp_path / "events.jsonl")[0])["digest"]
        # Later successful receptions must count even if the clock repeats and nothing differs.
        monkeypatch.setattr(console, "log_event", lambda *args, **kwargs:
                            events.log_event(*args, **kwargs, clock=lambda: 100.0))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: _hear(client, command="rtl_433 -d 1"), range(12)))
        assert _ism(client) == {**first, "count": 13}
        # Drop the first log files without deleting the earned badge.
        for _ in range(events.KEEP_ROTATED + 2):
            events.log_event(tmp_path / "events.jsonl", "tick", {}, max_bytes=1)
        assert all(event["kind"] != "reception" for event in
                   events.read_events(tmp_path / "events.jsonl"))
    with _client(tmp_path) as client:
        assert _ism(client) == {**first, "count": 13}


def test_startup_backfill_is_idempotent_and_recovers_an_orphan_even_after_clock_rewind(tmp_path):
    event = _event()
    log = tmp_path / "events.jsonl"
    events.log_event(log, "reception", event["payload"], clock=lambda: 123.25)
    with _client(tmp_path) as client:
        first = _ism(client)
        assert first["count"] == 1 and first["digest"] == _mint(event)["digest"]
    with _client(tmp_path) as client:
        assert _ism(client) == first
    # A line made it to the log before power loss, with the RTC moving backwards.
    events.log_event(log, "reception", {**event["payload"], "command": "rtl_433 -d 1"},
                     clock=lambda: 3)
    with _client(tmp_path) as client:
        assert _ism(client) == {**first, "count": 2}
    with _client(tmp_path) as client:
        assert _ism(client) == {**first, "count": 2}


@pytest.mark.parametrize("failure", ["log", "sqlite"])
def test_write_failures_keep_the_reader_alive_and_errors_clear(tmp_path, monkeypatch, failure):
    with _client(tmp_path) as client:
        assert _stamps(client)["error"] is None
        if failure == "sqlite":
            with closing(sqlite3.connect(tmp_path / "progress.db")) as db, db:
                db.execute("CREATE TRIGGER cannot_stamp BEFORE INSERT ON stamps "
                           "BEGIN SELECT RAISE(ABORT, 'badge drive full'); END")
        with monkeypatch.context() as patch:
            if failure == "log":
                def broken(*args, **kwargs):
                    raise OSError("event drive full")
                patch.setattr(console, "log_event", broken)
            _hear(client)
            assert _ism(client)["count"] == 0
            if failure == "log":
                assert _stamps(client)["error"] is None
                assert not client.get("/api/receptions").json()["receptions"]
                assert "event drive full" in client.get("/api/receptions").json()["error"]
            else:
                assert "badge drive full" in _stamps(client)["error"]
                # A failed stamp write never discards the reception that was logged.
                assert len(client.get("/api/receptions").json()["receptions"]) == 1
        if failure == "sqlite":
            with closing(sqlite3.connect(tmp_path / "progress.db")) as db, db:
                db.execute("DROP TRIGGER cannot_stamp")
        _hear(client, command="rtl_433 -d 1")
        row = _ism(client)
        assert row["count"] == (2 if failure == "sqlite" else 1)
        assert row["provenance"]["command"] == ("rtl_433 -d 0" if failure == "sqlite" else
                                              "rtl_433 -d 1")
        assert _stamps(client)["error"] is None
        assert client.get("/api/receptions").json()["error"] is None


def test_stamp_write_after_close_never_touches_the_connection(tmp_path):
    store = Progress(tmp_path / "progress.db")
    store.close()
    assert store.record_stamps([_mint(_event())]) is False
    store.close()
    with closing(sqlite3.connect(tmp_path / "progress.db")) as db, db:
        assert db.execute("SELECT count(*) FROM stamps").fetchone() == (0,)


def test_sstv_stamp_keeps_the_radio_selected_for_the_capture(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGINT101_RTLSDR_SERIAL", raising=False)
    devices = [RtlSdrDevice(4, "first-radio")]
    launches = []

    def spawn(freq, index, path):
        assert index == 4
        child = LateProc([SSTV, "-d", str(index)])
        launches.append((child, path))
        return child

    decoder = SstvDecoder(
        data_dir=tmp_path, output_dir=tmp_path / "sstv", spawn=spawn,
        which=lambda tool: tool, radio_present=lambda: True, list_devices=lambda: devices)
    with _client(tmp_path, sstv_factory=lambda: decoder) as client:
        decoder.start()
        child, path = launches[0]
        path.write_bytes(PNG_BYTES)
        devices[:] = [RtlSdrDevice(8, "replacement-radio")]
        decoder.device = 8  # Same mutation a later bus selection would make.
        _finish(child)
        decoder.stop()
        [event] = client.get("/api/receptions?instrument=sstv").json()["receptions"]
        assert event["payload"]["radio"] == {"kind": "rtl-sdr", "index": 4, "serial": "first-radio"}
        row = next(row for row in _stamps(client)["stamps"] if row["instrument"] == "sstv")
        assert row["count"] == 1 and row["provenance"]["radio"] == event["payload"]["radio"]


def test_logbook_lists_receivers_and_uses_distinct_marks(tmp_path):
    with _client(tmp_path) as client:
        assert '<a class="rack__log-link" href="/logbook"' in client.get("/operate/ism").text
        _hear(client, radio={"kind": "rtl-sdr", "index": 0, "serial": "receiver-serial"})
        client.app.state.lora.on_reception({}, radio={"kind": "meshtasticd", "host": "bridge:4403"})
        response = client.get("/logbook")
        assert response.status_code == 200
        html = response.text
        for key in KEYS:
            assert f'data-logbook-instrument="{key}"' in html
        for excluded in ("waterfall", "flock", "rayhunter", "health", "flipper", "clock"):
            assert f'data-logbook-instrument="{excluded}"' not in html
        assert 'class="btn-back" href="/operate"' in html
        assert 'class="logbook-mark logbook-mark--stamp">STAMP<' in html
        assert 'class="logbook-mark logbook-mark--replay">REPLAY<' in html
        assert "Not heard yet" in html and "None yet" in html
        assert "433.92 MHz" in html and "receiver-serial" in html and "rtl_433 -d 0" in html
        assert "bridge:4403" in html and '<time datetime="' in html


def test_only_the_live_hook_logs_receptions():
    """Regression pin: a replay or another route must not gain its own reception writer."""
    site = []
    for file in sorted(Path(console.__file__).parent.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        writers = {"log_event"}
        constants = {node.targets[0].id for node in tree.body
                     if isinstance(node, ast.Assign) and len(node.targets) == 1
                     and isinstance(node.targets[0], ast.Name)
                     and isinstance(node.value, ast.Constant) and node.value.value == "reception"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                writers.update(alias.asname or alias.name for alias in node.names
                               if alias.name == "log_event")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name not in writers:
                continue
            kinds = [*node.args[1:2], *(kw.value for kw in node.keywords if kw.arg == "kind")]
            if any(isinstance(kind, ast.Constant) and kind.value == "reception"
                   or isinstance(kind, ast.Name) and kind.id in constants for kind in kinds):
                site.append((file.name, node))
    [(name, call)] = site
    assert name == "app.py"
    tree = ast.parse(Path(console.__file__).read_text(encoding="utf-8"))
    heard = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == "_heard")
    assert heard.lineno < call.lineno <= heard.end_lineno


def test_event_watermark_retires_rotated_keys_without_resetting_badges(tmp_path):
    log = tmp_path / "events.jsonl"
    with _client(tmp_path) as client:
        _hear(client)
        first = _ism(client)
        for _ in range(events.KEEP_ROTATED + 2):
            events.log_event(log, "tick", {}, max_bytes=1)
        _hear(client, command="rtl_433 -d 1")
        assert _ism(client) == {**first, "count": 2}
    with closing(sqlite3.connect(tmp_path / "progress.db")) as db:
        assert db.execute("SELECT count(*) FROM stamp_events").fetchone() == (1,)
    with _client(tmp_path) as client:
        assert _ism(client) == {**first, "count": 2}


def test_backfill_counts_identical_legacy_lines_without_recounting_on_restart(tmp_path):
    event = _event()
    log = tmp_path / "events.jsonl"
    for _ in range(2):
        events.log_event(log, "reception", event["payload"], clock=lambda: event["ts"])
    with _client(tmp_path) as client:
        first = _ism(client)
        assert first["count"] == 2
        assert first["digest"] == _mint(event)["digest"]
    with _client(tmp_path) as client:
        assert _ism(client) == first


def test_a_reader_parked_before_credit_cannot_use_a_closed_store(tmp_path, monkeypatch):
    entered, resume = Event(), Event()
    with _client(tmp_path) as client, ThreadPoolExecutor(max_workers=1) as pool:
        store = client.app.state.progress
        original = store.record_stamps

        def paused(*args, **kwargs):
            entered.set()
            assert resume.wait(3)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "record_stamps", paused)
        reader = pool.submit(_hear, client)
        try:
            assert entered.wait(3)
            store.close()
        finally:
            resume.set()
        reader.result(3)
        assert client.app.state.stamp_error is None
        with closing(sqlite3.connect(tmp_path / "progress.db")) as db:
            assert db.execute("SELECT count(*) FROM stamps").fetchone() == (0,)
    # Its event was already logged, so a later open can still credit it.
    with _client(tmp_path) as client:
        assert _ism(client)["count"] == 1


def test_the_current_reception_is_credited_if_it_rotates_before_backfill(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    with _client(tmp_path) as client:
        def append_then_rotate(*args, **kwargs):
            event = events.log_event(*args, **kwargs)
            for _ in range(events.KEEP_ROTATED + 2):
                events.log_event(log, "tick", {}, max_bytes=1)
            return event

        monkeypatch.setattr(console, "log_event", append_then_rotate)
        _hear(client)
        assert all(event["kind"] != "reception" for event in events.read_events(log))
        assert _ism(client)["count"] == 1


def test_api_reports_a_write_error_even_when_badges_cannot_be_read(tmp_path, monkeypatch):
    with _client(tmp_path) as client:
        store = client.app.state.progress

        def cannot_write(*args, **kwargs):
            raise sqlite3.OperationalError("badge database is locked")

        def cannot_read():
            raise sqlite3.OperationalError("read failed")

        monkeypatch.setattr(store, "record_stamps", cannot_write)
        _hear(client)
        monkeypatch.setattr(store, "stamps", cannot_read)
        assert _stamps(client) == {
            "stamps": [], "error": "a reception badge could not be saved: badge database is locked",
        }
