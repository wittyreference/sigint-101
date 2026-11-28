# ABOUTME: Tests for Slice 1a-i's shared attempt layer (AttemptKind, Progress, app hooks).
# ABOUTME: Uses test-only attempt kinds and local tables to prove live vs replay separation.

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101 import app as app_mod
from sigint101 import challenges, events
from sigint101.progress import Progress


def _create_bench_tables(db_path: Path) -> None:
    """Create test-only replay_answer tables inside a test database (never production schema)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS test_bench_attempts (
                bench_id   TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                window_s   INTEGER NOT NULL,
                attempt_id TEXT NOT NULL,
                recording  TEXT NOT NULL,
                digest     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS test_bench_passes (
                bench_id   TEXT NOT NULL,
                started_at REAL NOT NULL,
                attempt_id TEXT NOT NULL,
                passed_at  REAL NOT NULL,
                recording  TEXT NOT NULL,
                digest     TEXT NOT NULL,
                answer     TEXT NOT NULL,
                PRIMARY KEY (bench_id, attempt_id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _create_slotted_tables(db_path: Path) -> None:
    """Create test-only slotted live_reception tables inside a test database."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS test_slotted_attempts (
                hunt_id    TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                window_s   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS test_slotted_passes (
                hunt_id    TEXT NOT NULL,
                started_at REAL NOT NULL,
                band_slot  INTEGER NOT NULL,
                passed_at  REAL NOT NULL,
                instrument TEXT NOT NULL,
                provenance TEXT NOT NULL,
                digest     TEXT NOT NULL,
                PRIMARY KEY (hunt_id, started_at, band_slot)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _bench_kind():
    return challenges.AttemptKind(
        kind="test_bench",
        attempts_table="test_bench_attempts",
        passes_table="test_bench_passes",
        key_column="bench_id",
        slot_column=None,
        identity_column="attempt_id",
        start_event="test_bench_start",
        pass_event="test_bench_pass",
        credit="replay_answer",
        attempt_columns=("recording", "digest"),
        pass_columns=("passed_at", "recording", "digest", "answer"),
        label="bench replay",
    )


def _slotted_live_kind():
    return challenges.AttemptKind(
        kind="test_slotted",
        attempts_table="test_slotted_attempts",
        passes_table="test_slotted_passes",
        key_column="hunt_id",
        slot_column="band_slot",
        start_event="test_slotted_start",
        pass_event="test_slotted_pass",
        credit="live_reception",
        label="slotted hunt",
    )


def _lora_item(from_id: str = "!a1b2c3d4", text: str = "ping") -> dict:
    return {"from_id": from_id, "text": text, "snr": 6.5, "rssi": -88}


def _patch_log_clock(monkeypatch, now_box):
    real_log = events.log_event
    monkeypatch.setattr(
        app_mod,
        "log_event",
        lambda *args, **kwargs: real_log(*args, **kwargs, clock=lambda: now_box[0]),
    )


def _make_stamp(instrument: str, ts: float, digest: str, *, event_id: str = "e1") -> dict:
    return {
        "instrument": instrument,
        "first_ts": ts,
        "provenance": {"tool": "meshtasticd", "freq_hz": None, "radio": {"kind": "meshtasticd"}},
        "digest": digest,
        "event_id": event_id,
    }


def test_attempt_kind_validation_and_derived_replay_flags():
    """AttemptKind validates identifiers/credit modes and derives replay_flags from credit."""
    assert challenges.CHALLENGE_KIND.kind == "challenge"
    assert challenges.CHALLENGE_KIND.credit == "live_reception"
    assert challenges.CHALLENGE_KIND.payload_key == "unit"
    assert challenges.CHALLENGE_KIND.replay_flags() == {}

    bench = _bench_kind()
    assert bench.credit == "replay_answer"
    assert bench.attempt_columns == ("recording", "digest")
    assert bench.replay_flags() == {"replay": True, "source": "replay"}

    with pytest.raises(ValueError, match="credit"):
        challenges.AttemptKind(
            kind="bad_credit",
            attempts_table="a",
            passes_table="p",
            key_column="k",
            slot_column=None,
            start_event="s",
            pass_event="e",
            credit="hybrid",  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="identifier"):
        challenges.AttemptKind(
            kind="bad_sql",
            attempts_table="a; DROP TABLE challenges",
            passes_table="p",
            key_column="k",
            slot_column=None,
            start_event="s",
            pass_event="e",
            credit="live_reception",
        )

    with pytest.raises(ValueError, match="attempt column"):
        challenges.AttemptKind(
            kind="bad_col",
            attempts_table="a",
            passes_table="p",
            key_column="k",
            slot_column=None,
            start_event="s",
            pass_event="e",
            credit="replay_answer",
            attempt_columns=("started_at",),
        )

    with pytest.raises(ValueError, match="passed_at"):
        challenges.AttemptKind(
            kind="no_passed_at",
            attempts_table="a",
            passes_table="p",
            key_column="k",
            slot_column=None,
            start_event="s",
            pass_event="e",
            credit="replay_answer",
            pass_columns=("answer",),
        )

    with pytest.raises(ValueError, match="json_column"):
        challenges.AttemptKind(
            kind="bad_json_col",
            attempts_table="a",
            passes_table="p",
            key_column="k",
            slot_column=None,
            start_event="s",
            pass_event="e",
            credit="replay_answer",
            pass_columns=("passed_at", "answer"),
            json_columns=("missing_col",),
        )


def test_in_window_and_attempt_status_helpers():
    """Shared in_window and attempt_status helpers handle inclusive bounds, clock move, and pass."""
    attempt = {"started_at": 1000.0, "window_s": 900}
    assert challenges.in_window(attempt, {"first_ts": 1000.0}) is True
    assert challenges.in_window(attempt, {"first_ts": 1900.0}) is True
    assert challenges.in_window(attempt, {"first_ts": 999.999}) is False
    assert challenges.in_window(attempt, {"first_ts": 1900.001}) is False
    assert challenges.in_window(attempt, {"first_ts": True}) is False
    assert challenges.in_window(attempt, {"first_ts": "1200"}) is False
    assert challenges.in_window(attempt, None) is False

    idle = challenges.attempt_status(900, None, None, 1200.0)
    assert idle["state"] == "idle"
    assert idle["window_s"] == 900

    active = challenges.attempt_status(900, attempt, None, 1250.5)
    assert active["state"] == "active"
    assert active["remaining_s"] == 649.5

    moved = challenges.attempt_status(900, attempt, None, 950.0)
    assert moved["state"] == "expired"
    assert moved["reason"] == "clock_moved"

    expired = challenges.attempt_status(900, attempt, None, 1901.0)
    assert expired["state"] == "expired"
    assert expired["reason"] is None

    # attempt_pass for current started_at wins over a newer-timestamped older pass.
    passed = challenges.attempt_status(
        900,
        attempt,
        {"started_at": 500.0},
        1950.0,
        attempt_pass={"started_at": 1000.0},
    )
    assert passed["state"] == "passed"
    assert passed["remaining_s"] == 0


def test_progress_attempt_extra_columns_and_insert_once_pass_on_replay_kind(tmp_path):
    """Progress carries extra columns, writes insert-once passes, and refuses replay credit."""
    db_path = tmp_path / "progress.db"
    prog = Progress(db_path)
    try:
        _create_bench_tables(db_path)
        bench = _bench_kind()

        # Missing or unexpected extra columns fail loudly before writing.
        with pytest.raises(ValueError, match="attempt columns"):
            prog.start_attempt(bench, "b1", 1000.0, 600, recording="clip-1.sigmf")
        with pytest.raises(ValueError, match="attempt columns"):
            prog.start_attempt(
                bench, "b1", 1000.0, 600,
                recording="clip-1.sigmf", digest="d1", bogus="nope",
            )

        assert prog.start_attempt(
            bench, "b1", 1000.0, 600, recording="clip-1.sigmf", digest="d1",
        ) is True
        first = prog.attempt(bench, "b1")
        assert first["attempt"]["bench_id"] == "b1"
        assert first["attempt"]["started_at"] == 1000.0
        assert first["attempt"]["window_s"] == 600
        assert first["attempt"]["recording"] == "clip-1.sigmf"
        assert first["attempt"]["digest"] == "d1"
        assert isinstance(first["attempt"]["attempt_id"], str) and first["attempt"]["attempt_id"]

        # Overwrite and restore previous row including extra columns.
        assert prog.start_attempt(
            bench, "b1", 1200.0, 600, recording="clip-2.sigmf", digest="d2",
        ) is True
        assert prog.restore_attempt(bench, "b1", first["attempt"]) is True
        assert prog.attempt(bench, "b1")["attempt"] == first["attempt"]

        # Restore None deletes the row.
        assert prog.restore_attempt(bench, "b1", None) is True
        assert prog.attempt(bench, "b1")["attempt"] is None

        # Re-start and test shared insert-once record_attempt_pass.
        assert prog.start_attempt(
            bench, "b1", 1000.0, 600, recording="clip-1.sigmf", digest="d1",
        ) is True
        aid = prog.attempt(bench, "b1")["attempt"]["attempt_id"]
        pass_one = {
            "passed_at": 1050.0,
            "recording": "clip-1.sigmf",
            "digest": "d1",
            "answer": "fm_broadcast",
        }
        pass_two = {
            "passed_at": 1099.0,
            "recording": "clip-1.sigmf",
            "digest": "d1",
            "answer": "changed_answer",
        }
        assert prog.record_attempt_pass(
            bench, "b1", 1000.0, None, pass_one, attempt_id=aid,
        ) is True
        assert prog.record_attempt_pass(
            bench, "b1", 1000.0, None, pass_two, attempt_id=aid,
        ) is False

        saved = prog.attempt(bench, "b1")
        assert saved["passes"] == 1
        assert saved["attempt_pass"]["answer"] == "fm_broadcast"
        assert saved["attempt_pass"]["passed_at"] == 1050.0

        # credit_attempts refuses replay_answer kinds (fail-closed).
        with pytest.raises(ValueError, match="live_reception"):
            prog.credit_attempts(bench, [_make_stamp("lora", 1050.0, "abc")], lambda a, s: True)
    finally:
        prog.close()

    # Closed store returns False without writing.
    assert prog.start_attempt(
        _bench_kind(), "b1", 2000.0, 600, recording="x", digest="y",
    ) is False
    assert prog.record_attempt_pass(
        _bench_kind(), "b1", 2000.0, None,
        {"passed_at": 2010.0, "recording": "x", "digest": "y", "answer": "a"},
        attempt_id="closed-aid",
    ) is False


def test_progress_slotted_live_kind_uses_shared_insert_once_pass(tmp_path):
    """Slotted live_reception kinds credit multiple slots (including slot 0) insert-once."""
    db_path = tmp_path / "progress.db"
    prog = Progress(db_path)
    try:
        _create_slotted_tables(db_path)
        slotted = _slotted_live_kind()

        assert prog.start_attempt(slotted, "cap-1", 1000.0, 1800) is True

        def qualifies(attempt, stamp):
            if not challenges.in_window(attempt, stamp):
                return None
            if stamp["instrument"] == "rds":
                return 0
            if stamp["instrument"] == "lora":
                return 1
            return None

        stamps = [
            _make_stamp("rds", 1010.0, "d-rds-1", event_id="e1"),
            _make_stamp("rds", 1020.0, "d-rds-2", event_id="e2"),  # duplicate slot 0
            _make_stamp("lora", 1030.0, "d-lora-1", event_id="e3"),
        ]
        credited = prog.credit_attempts(slotted, stamps, qualifies)
        assert [(c["hunt_id"], c["band_slot"], c["digest"]) for c in credited] == [
            ("cap-1", 0, "d-rds-1"),
            ("cap-1", 1, "d-lora-1"),
        ]

        # Re-crediting same stamps is a no-op (insert-once).
        assert prog.credit_attempts(slotted, stamps, qualifies) == []

        saved = prog.attempt(slotted, "cap-1")
        assert saved["passes"] == 2
        assert [p["band_slot"] for p in saved["attempt_passes"]] == [0, 1]
        assert saved["attempt_passes"][0]["digest"] == "d-rds-1"

        # Slot argument validation: slotted requires slot, single-slot rejects slot.
        with pytest.raises(ValueError, match="slot"):
            prog.record_attempt_pass(
                slotted, "cap-1", 1000.0, None,
                {"passed_at": 1040.0, "instrument": "rds", "provenance": {}, "digest": "x"},
            )
        with pytest.raises(ValueError, match="slot"):
            prog.record_attempt_pass(
                challenges.CHALLENGE_KIND, 9, 1000.0, 0,
                {"passed_at": 1040.0, "instrument": "lora", "provenance": {}, "digest": "x"},
            )
    finally:
        prog.close()


def test_app_replay_kind_never_credited_by_heard_or_backfill_and_derives_replay_flags(
    tmp_path, monkeypatch
):
    """_heard and _backfill_stamps ignore replay_answer kinds; Start/pass derive replay flags."""
    now = [1000.0]
    _patch_log_clock(monkeypatch, now)
    bench = _bench_kind()
    tripwire_calls: list[tuple] = []

    def tripwire(attempt, stamp):
        tripwire_calls.append((attempt, stamp))
        return True

    # Pre-initialize progress.db with the test-only bench tables before lifespan startup backfill.
    prog_init = Progress(tmp_path / "progress.db")
    prog_init.close()
    _create_bench_tables(tmp_path / "progress.db")

    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app.state.register_attempt_kind(bench, qualifies=tripwire)

    with TestClient(app) as client:
        # Start Tier B unit 9 and the test-only replay_answer attempt.
        r = client.post("/api/challenges/9/start")
        assert r.status_code == 200

        # Even if a caller passes conflicting replay/source flags, kind.credit wins.
        started_at, err = app.state.start_attempt(
            bench,
            "bench-u4",
            600,
            extra={"recording": "u4-fm.sigmf", "digest": "deadbeef"},
            event_fields={"replay": False, "source": "live", "note": "u4"},
        )
        assert err is None
        assert started_at == 1000.0

        # Emit a live LoRa reception via the real receiver hook.
        now[0] = 1015.0
        app.state.lora.on_reception(_lora_item())

        # Tier B unit 9 passed; replay_answer kind was never called and never credited.
        assert client.get("/api/challenges/9").json()["state"] == "passed"
        assert tripwire_calls == []
        assert app.state.progress.attempt(bench, "bench-u4")["passes"] == 0

    # Reopen the app from disk so startup _backfill_stamps runs over events.jsonl.
    app2 = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app2.state.register_attempt_kind(bench, qualifies=tripwire)

    with TestClient(app2):
        assert tripwire_calls == []
        assert app2.state.progress.attempt(bench, "bench-u4")["passes"] == 0

        # Record a replay answer pass through the shared insert-once path.
        bench_aid = app2.state.progress.attempt(bench, "bench-u4")["attempt"]["attempt_id"]
        inserted, pass_err = app2.state.record_attempt_pass(
            bench,
            "bench-u4",
            1000.0,
            None,
            {
                "passed_at": 1030.0,
                "recording": "u4-fm.sigmf",
                "digest": "deadbeef",
                "answer": "fm_broadcast",
            },
            attempt_id=bench_aid,
            event_fields={"replay": False, "source": "live"},
        )
        assert inserted is True
        assert pass_err is None

        # Duplicate replay pass is an insert-once no-op and logs no second event.
        inserted_dup, dup_err = app2.state.record_attempt_pass(
            bench,
            "bench-u4",
            1000.0,
            None,
            {
                "passed_at": 1040.0,
                "recording": "u4-fm.sigmf",
                "digest": "deadbeef",
                "answer": "other",
            },
            attempt_id=bench_aid,
        )
        assert inserted_dup is False
        assert dup_err is None

    evts = events.read_events(tmp_path / "events.jsonl")
    bench_starts = [e for e in evts if e.get("kind") == "test_bench_start"]
    bench_passes = [e for e in evts if e.get("kind") == "test_bench_pass"]
    chal_starts = [e for e in evts if e.get("kind") == "challenge_start"]
    chal_passes = [e for e in evts if e.get("kind") == "challenge_pass"]

    assert len(bench_starts) == 1
    assert bench_starts[0]["payload"]["bench_id"] == "bench-u4"
    assert bench_starts[0]["payload"]["recording"] == "u4-fm.sigmf"
    assert bench_starts[0]["payload"]["digest"] == "deadbeef"
    assert bench_starts[0]["payload"]["replay"] is True
    assert bench_starts[0]["payload"]["source"] == "replay"
    assert bench_starts[0]["payload"]["note"] == "u4"

    assert len(bench_passes) == 1
    assert bench_passes[0]["payload"]["bench_id"] == "bench-u4"
    assert bench_passes[0]["payload"]["answer"] == "fm_broadcast"
    assert bench_passes[0]["payload"]["replay"] is True
    assert bench_passes[0]["payload"]["source"] == "replay"

    # Live-reception events never carry replay flags.
    assert len(chal_starts) == 1
    assert "replay" not in chal_starts[0]["payload"]
    assert "source" not in chal_starts[0]["payload"]
    assert len(chal_passes) == 1
    assert "replay" not in chal_passes[0]["payload"]
    assert "source" not in chal_passes[0]["payload"]


def test_app_start_attempt_launch_and_log_failure_rollback_with_extra_columns(
    tmp_path, monkeypatch
):
    """Failing launch or log_event restores previous attempt (extra cols) and logs no start."""
    now = [2000.0]
    bench = _bench_kind()

    prog_init = Progress(tmp_path / "progress.db")
    prog_init.close()
    _create_bench_tables(tmp_path / "progress.db")

    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app.state.register_attempt_kind(bench)

    with TestClient(app):
        order: list[str] = []

        # Clean first Start runs pre_start before launch.
        _, err = app.state.start_attempt(
            bench,
            "b1",
            600,
            extra={"recording": "rec-1.sigmf", "digest": "aaa"},
            pre_start=lambda: order.append("pre_start"),
            launch=lambda: order.append("launch"),
        )
        assert err is None
        assert order == ["pre_start", "launch"]
        first_aid = app.state.progress.attempt(bench, "b1")["attempt"]["attempt_id"]

        # Restart with failing launch rolls back to rec-1.sigmf / aaa and logs no second start.
        now[0] = 2100.0

        def boom():
            raise RuntimeError("replay stream failed")

        _, err = app.state.start_attempt(
            bench,
            "b1",
            600,
            extra={"recording": "rec-2.sigmf", "digest": "bbb"},
            launch=boom,
        )
        assert err is not None and "replay stream failed" in err
        assert app.state.attempt_start_errors[("test_bench", "b1")] == err
        assert app.state.progress.attempt(bench, "b1")["attempt"] == {
            "bench_id": "b1",
            "started_at": 2000.0,
            "window_s": 600,
            "attempt_id": first_aid,
            "recording": "rec-1.sigmf",
            "digest": "aaa",
        }

        # Brand-new key with failing launch deletes the inserted row and logs no start.
        _, err2 = app.state.start_attempt(
            bench,
            "b2",
            600,
            extra={"recording": "rec-x.sigmf", "digest": "xxx"},
            launch=boom,
        )
        assert err2 is not None
        assert app.state.progress.attempt(bench, "b2")["attempt"] is None

        # log_event OSError rolls back identically to previous row with extra columns.
        real_log = app_mod.log_event

        def fail_start_log(path, kind, payload, **kwargs):
            if kind == "test_bench_start":
                raise OSError("disk full")
            return real_log(path, kind, payload, **kwargs)

        monkeypatch.setattr(app_mod, "log_event", fail_start_log)
        now[0] = 2200.0
        _, err3 = app.state.start_attempt(
            bench,
            "b1",
            600,
            extra={"recording": "rec-3.sigmf", "digest": "ccc"},
        )
        assert err3 is not None and "disk full" in err3
        assert app.state.attempt_start_errors[("test_bench", "b1")] == err3
        assert app.state.progress.attempt(bench, "b1")["attempt"] == {
            "bench_id": "b1",
            "started_at": 2000.0,
            "window_s": 600,
            "attempt_id": first_aid,
            "recording": "rec-1.sigmf",
            "digest": "aaa",
        }

        # Restoring log_event and starting cleanly clears the recorded start error.
        monkeypatch.setattr(app_mod, "log_event", real_log)
        now[0] = 2300.0
        _, err4 = app.state.start_attempt(
            bench,
            "b1",
            600,
            extra={"recording": "rec-4.sigmf", "digest": "ddd"},
        )
        assert err4 is None
        assert ("test_bench", "b1") not in app.state.attempt_start_errors
        assert app.state.progress.attempt(bench, "b1")["attempt"]["recording"] == "rec-4.sigmf"

    starts = [
        e for e in events.read_events(tmp_path / "events.jsonl")
        if e.get("kind") == "test_bench_start"
    ]
    assert [e["payload"]["recording"] for e in starts] == ["rec-1.sigmf", "rec-4.sigmf"]


def test_app_second_live_kind_shares_heard_backfill_clock_gate_and_isolates_errors(
    tmp_path, monkeypatch
):
    """A second live_reception kind shares _heard, backfill clock gate, and isolates DB errors."""
    now = [2000.0]
    _patch_log_clock(monkeypatch, now)
    slotted = _slotted_live_kind()

    def slotted_qualifies(attempt, stamp):
        if stamp.get("instrument") == "lora" and challenges.in_window(attempt, stamp):
            return 0
        return None

    prog_init = Progress(tmp_path / "progress.db")
    prog_init.close()
    _create_slotted_tables(tmp_path / "progress.db")

    # Seed events.jsonl with a LoRa reception at ts=2000 BEFORE a backward clock step to ts=1500
    # where the slotted hunt starts; backfill must NOT credit the pre-Start reception.
    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app.state.register_attempt_kind(slotted, qualifies=slotted_qualifies)

    with TestClient(app):
        # 1. Pre-Start LoRa reception at ts=2000.
        app.state.lora.on_reception(_lora_item(from_id="!pre0001"))

        # 2. Clock steps backward to 1500; start the slotted hunt.
        now[0] = 1500.0
        _, err = app.state.start_attempt(
            slotted, "hunt-1", 900,
            event_fields={"replay": True, "source": "replay"},  # stripped for live_reception
        )
        assert err is None
        assert app.state.progress.attempt(slotted, "hunt-1")["passes"] == 0

    # 3. Reopen to run startup _backfill_stamps: pre-Start reception (ts=2000) is inside
    # [1500, 2400], but logged before test_slotted_start, so append-order gate rejects it.
    app2 = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app2.state.register_attempt_kind(slotted, qualifies=slotted_qualifies)
    with TestClient(app2) as client2:
        assert app2.state.progress.attempt(slotted, "hunt-1")["passes"] == 0

        # 4. Now start Tier B unit 9 too, and simulate a DB failure ONLY on the slotted kind
        # during _heard: Tier B and reception stamps must still save!
        now[0] = 1600.0
        assert client2.post("/api/challenges/9/start").status_code == 200

        real_credit = app2.state.progress.credit_attempts

        def fail_slotted_only(kind, stamps_in, qfn):
            if kind.kind == slotted.kind:
                raise sqlite3.OperationalError("slotted table locked")
            return real_credit(kind, stamps_in, qfn)

        monkeypatch.setattr(app2.state.progress, "credit_attempts", fail_slotted_only)
        now[0] = 1610.0
        app2.state.lora.on_reception(_lora_item(from_id="!post001"))

        assert client2.get("/api/challenges/9").json()["state"] == "passed"
        assert any(s["instrument"] == "lora" for s in app2.state.progress.stamps())
        assert "slotted table locked" in (app2.state.attempt_errors.get("test_slotted") or "")
        assert app2.state.progress.attempt(slotted, "hunt-1")["passes"] == 0

        # 5. GET /api/challenges/9 writes nothing to SQLite or events.jsonl.
        writes: list[int] = []

        def authorizer(action, arg1, arg2, db_name, trigger):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                writes.append(action)
            return sqlite3.SQLITE_OK

        before_events = (tmp_path / "events.jsonl").read_bytes()
        app2.state.progress._conn.set_authorizer(authorizer)
        try:
            r = client2.get("/api/challenges/9")
            assert r.status_code == 200
        finally:
            app2.state.progress._conn.set_authorizer(None)
        assert writes == []
        assert (tmp_path / "events.jsonl").read_bytes() == before_events

    # 6. Reopen cleanly: startup backfill recovers the post-Start slotted pass while still
    # ignoring the pre-Start reception.
    app3 = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app3.state.register_attempt_kind(slotted, qualifies=slotted_qualifies)
    with TestClient(app3):
        saved = app3.state.progress.attempt(slotted, "hunt-1")
        assert saved["passes"] == 1
        assert saved["attempt_passes"][0]["band_slot"] == 0

    evts = events.read_events(tmp_path / "events.jsonl")
    slotted_starts = [e for e in evts if e.get("kind") == "test_slotted_start"]
    slotted_passes = [e for e in evts if e.get("kind") == "test_slotted_pass"]
    assert len(slotted_starts) == 1
    assert "replay" not in slotted_starts[0]["payload"]
    assert "source" not in slotted_starts[0]["payload"]
    assert len(slotted_passes) == 1
    assert slotted_passes[0]["payload"]["band_slot"] == 0
    assert "replay" not in slotted_passes[0]["payload"]
    assert "source" not in slotted_passes[0]["payload"]
    assert json.loads(json.dumps(slotted_passes[0]))["payload"]["hunt_id"] == "hunt-1"


def test_f1_malformed_list_kind_in_journal_is_skipped_during_backfill(tmp_path):
    """F1: a list-valued event kind in events.jsonl is skipped rather than aborting backfill."""
    prog = Progress(tmp_path / "progress.db")
    try:
        assert prog.start_challenge(4, 1000.0, 900) is True
    finally:
        prog.close()

    journal = tmp_path / "events.jsonl"
    lines = [
        json.dumps({
            "ts": 1000.0,
            "kind": "challenge_start",
            "payload": {"unit": 4, "started_at": 1000.0, "window_s": 900},
        }),
        json.dumps({
            "ts": 1005.0,
            "kind": ["malformed", "list"],
            "payload": {},
        }),
        json.dumps({
            "ts": 1010.0,
            "kind": "reception",
            "event_id": "ev-f1-1",
            "payload": {
                "instrument": "ism",
                "tool": "rtl_433",
                "freq_hz": 433_920_000,
                "command": "rtl_433 -d 0",
                "radio": {"kind": "rtl-sdr", "index": 0, "serial": None},
                "item": {"model": "Test"},
            },
        }),
    ]
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")

    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: 1020.0)
    with TestClient(app) as client:
        body = client.get("/api/challenges/4").json()
        assert body["state"] == "passed"
        assert body["passes"] == 1


def test_f2_queued_or_superseded_pass_cannot_credit_missing_or_failed_start(
    tmp_path, monkeypatch
):
    """F2: pass write verifies the matching attempt still exists in the same transaction."""
    now = [2000.0]
    _patch_log_clock(monkeypatch, now)
    bench = _bench_kind()

    prog_init = Progress(tmp_path / "progress.db")
    prog_init.close()
    _create_bench_tables(tmp_path / "progress.db")

    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app.state.register_attempt_kind(bench)

    with TestClient(app):
        entered = threading.Event()
        release = threading.Event()
        start_result: dict = {}
        pass_result: dict = {}

        def paused_failing_launch():
            entered.set()
            release.wait(timeout=5.0)
            raise RuntimeError("replay stream aborted")

        def run_start():
            start_result["out"] = app.state.start_attempt(
                bench,
                "b-race",
                600,
                extra={"recording": "race.sigmf", "digest": "d-race"},
                launch=paused_failing_launch,
            )

        def run_pass():
            pass_result["out"] = app.state.record_attempt_pass(
                bench,
                "b-race",
                2000.0,
                None,
                {
                    "passed_at": 2005.0,
                    "recording": "race.sigmf",
                    "digest": "d-race",
                    "answer": "fm_broadcast",
                },
                attempt_id="race-attempt-id",
            )

        t_start = threading.Thread(target=run_start)
        t_start.start()
        assert entered.wait(timeout=5.0)

        t_pass = threading.Thread(target=run_pass)
        t_pass.start()
        t_pass.join(timeout=0.2)
        release.set()
        t_start.join(timeout=5.0)
        t_pass.join(timeout=5.0)

        assert start_result["out"][1] is not None
        assert pass_result["out"] == (False, None)
        saved = app.state.progress.attempt(bench, "b-race")
        assert saved["attempt"] is None
        assert saved["passes"] == 0

        # Superseded Start: submitting a pass for an older started_at must be rejected.
        now[0] = 3000.0
        _, err1 = app.state.start_attempt(
            bench, "b-sup", 600, extra={"recording": "r1.sigmf", "digest": "d1"}
        )
        assert err1 is None
        aid1 = app.state.progress.attempt(bench, "b-sup")["attempt"]["attempt_id"]
        now[0] = 3100.0
        _, err2 = app.state.start_attempt(
            bench, "b-sup", 600, extra={"recording": "r2.sigmf", "digest": "d2"}
        )
        assert err2 is None
        aid2 = app.state.progress.attempt(bench, "b-sup")["attempt"]["attempt_id"]

        stale_ins, stale_err = app.state.record_attempt_pass(
            bench,
            "b-sup",
            3000.0,
            None,
            {"passed_at": 3110.0, "recording": "r1.sigmf", "digest": "d1", "answer": "a"},
            attempt_id=aid1,
        )
        assert (stale_ins, stale_err) == (False, None)
        assert app.state.progress.attempt(bench, "b-sup")["passes"] == 0

        # Matching current Start succeeds once (insert-once preserved).
        ok_ins, ok_err = app.state.record_attempt_pass(
            bench,
            "b-sup",
            3100.0,
            None,
            {"passed_at": 3120.0, "recording": "r2.sigmf", "digest": "d2", "answer": "b"},
            attempt_id=aid2,
        )
        dup_ins, dup_err = app.state.record_attempt_pass(
            bench,
            "b-sup",
            3100.0,
            None,
            {"passed_at": 3125.0, "recording": "r2.sigmf", "digest": "d2", "answer": "c"},
            attempt_id=aid2,
        )
        assert (ok_ins, ok_err) == (True, None)
        assert (dup_ins, dup_err) == (False, None)
        assert app.state.progress.attempt(bench, "b-sup")["passes"] == 1

    evts = events.read_events(tmp_path / "events.jsonl")
    assert [e for e in evts if e.get("payload", {}).get("bench_id") == "b-race"] == []
    sup_passes = [
        e for e in evts
        if e.get("kind") == "test_bench_pass" and e.get("payload", {}).get("bench_id") == "b-sup"
    ]
    assert len(sup_passes) == 1 and sup_passes[0]["payload"]["started_at"] == 3100.0


def test_f3_failed_launch_with_broken_restore_never_leaves_start_eligible(
    tmp_path, monkeypatch
):
    """F3: a failed Start stays ineligible (even if restore fails) both live and after restart."""
    now = [1000.0]
    _patch_log_clock(monkeypatch, now)
    slotted = _slotted_live_kind()

    def slotted_qualifies(attempt, stamp):
        if stamp.get("instrument") == "lora" and challenges.in_window(attempt, stamp):
            return 0
        return None

    prog_init = Progress(tmp_path / "progress.db")
    prog_init.close()
    _create_slotted_tables(tmp_path / "progress.db")

    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app.state.register_attempt_kind(slotted, qualifies=slotted_qualifies)

    def boom():
        raise RuntimeError("hardware launch failed")

    with TestClient(app):
        # Case 1: launch fails while restore_attempt also raises sqlite3.OperationalError.
        real_restore = app.state.progress.restore_attempt

        def broken_restore(*_args, **_kwargs):
            raise sqlite3.OperationalError("restore failed")

        monkeypatch.setattr(app.state.progress, "restore_attempt", broken_restore)
        _, err = app.state.start_attempt(slotted, "hunt-fail", 900, launch=boom)
        assert err is not None and "hardware launch failed" in err
        assert app.state.progress.attempt(slotted, "hunt-fail")["attempt"] is None

        # A subsequent qualifying live reception must NOT credit the failed Start.
        now[0] = 1015.0
        app.state.lora.on_reception(_lora_item(from_id="!f3-bad"))
        assert app.state.progress.attempt(slotted, "hunt-fail")["passes"] == 0

        # Case 2: normal restore path preserves an earlier Start when a restart launch fails.
        monkeypatch.setattr(app.state.progress, "restore_attempt", real_restore)
        now[0] = 1100.0
        _, ok_err = app.state.start_attempt(slotted, "hunt-prev", 900)
        assert ok_err is None
        now[0] = 1200.0
        _, fail_err = app.state.start_attempt(slotted, "hunt-prev", 900, launch=boom)
        assert fail_err is not None
        assert app.state.progress.attempt(slotted, "hunt-prev")["attempt"]["started_at"] == 1100.0

    # Reopen from disk (restart): rejected Start stays ineligible across backfill too.
    app2 = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app2.state.register_attempt_kind(slotted, qualifies=slotted_qualifies)
    with TestClient(app2):
        assert app2.state.progress.attempt(slotted, "hunt-fail")["attempt"] is None
        assert app2.state.progress.attempt(slotted, "hunt-fail")["passes"] == 0
        assert app2.state.progress.attempt(slotted, "hunt-prev")["attempt"]["started_at"] == 1100.0


def test_f4_explicit_record_attempt_pass_rejects_live_reception_kind(tmp_path):
    """F4: _record_attempt_pass accepts only replay_answer kinds, never live_reception."""
    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: 1000.0)
    with TestClient(app) as client:
        assert client.post("/api/challenges/4/start").status_code == 200
        started_at = client.get("/api/challenges/4").json()["started_at"]

        with pytest.raises(ValueError, match="replay_answer"):
            app.state.record_attempt_pass(
                challenges.CHALLENGE_KIND,
                4,
                started_at,
                None,
                {
                    "passed_at": 1010.0,
                    "instrument": "ism",
                    "provenance": {},
                    "digest": "fabricated",
                },
            )

        body = client.get("/api/challenges/4").json()
        assert body["state"] == "active"
        assert body["passes"] == 0

    evts = events.read_events(tmp_path / "events.jsonl")
    assert [e for e in evts if e.get("kind") == "challenge_pass"] == []


def test_f5_reserved_replay_and_source_flags_win_over_descriptor_columns_key_and_slot(
    tmp_path, monkeypatch
):
    """F5: replay and source flags are reserved across columns, key, slot, and event_fields."""
    now = [4000.0]
    _patch_log_clock(monkeypatch, now)

    live_cols = challenges.AttemptKind(
        kind="live_flag_cols",
        attempts_table="test_live_flag_attempts",
        passes_table="test_live_flag_passes",
        key_column="item_id",
        slot_column=None,
        start_event="live_flag_start",
        pass_event="live_flag_pass",
        credit="live_reception",
        attempt_columns=("replay", "source"),
        pass_columns=("passed_at", "instrument", "provenance", "digest"),
    )
    live_key_slot = challenges.AttemptKind(
        kind="live_flag_ks",
        attempts_table="test_live_ks_attempts",
        passes_table="test_live_ks_passes",
        key_column="replay",
        slot_column="source",
        start_event="live_ks_start",
        pass_event="live_ks_pass",
        credit="live_reception",
        event_key_field="ks_key",
    )
    replay_cols = challenges.AttemptKind(
        kind="replay_flag_cols",
        attempts_table="test_replay_flag_attempts",
        passes_table="test_replay_flag_passes",
        key_column="source",
        slot_column="replay",
        identity_column="attempt_id",
        start_event="replay_flag_start",
        pass_event="replay_flag_pass",
        credit="replay_answer",
        attempt_columns=("recording", "note"),
        pass_columns=("passed_at", "recording", "answer"),
        event_key_field="rf_key",
    )

    prog_init = Progress(tmp_path / "progress.db")
    prog_init.close()
    conn = sqlite3.connect(tmp_path / "progress.db")
    try:
        conn.executescript(
            """
            CREATE TABLE test_live_flag_attempts (
                item_id TEXT PRIMARY KEY, started_at REAL NOT NULL, window_s INTEGER NOT NULL,
                replay INTEGER NOT NULL, source TEXT NOT NULL
            );
            CREATE TABLE test_live_flag_passes (
                item_id TEXT NOT NULL, started_at REAL NOT NULL, passed_at REAL NOT NULL,
                instrument TEXT NOT NULL, provenance TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY (item_id, started_at)
            );
            CREATE TABLE test_live_ks_attempts (
                replay TEXT PRIMARY KEY, started_at REAL NOT NULL, window_s INTEGER NOT NULL
            );
            CREATE TABLE test_live_ks_passes (
                replay TEXT NOT NULL, started_at REAL NOT NULL, source INTEGER NOT NULL,
                passed_at REAL NOT NULL, instrument TEXT NOT NULL, provenance TEXT NOT NULL,
                digest TEXT NOT NULL, PRIMARY KEY (replay, started_at, source)
            );
            CREATE TABLE test_replay_flag_attempts (
                source TEXT PRIMARY KEY, started_at REAL NOT NULL, window_s INTEGER NOT NULL,
                attempt_id TEXT NOT NULL, recording TEXT NOT NULL, note TEXT NOT NULL
            );
            CREATE TABLE test_replay_flag_passes (
                source TEXT NOT NULL, started_at REAL NOT NULL, attempt_id TEXT NOT NULL,
                replay INTEGER NOT NULL, passed_at REAL NOT NULL, recording TEXT NOT NULL,
                answer TEXT NOT NULL,
                PRIMARY KEY (source, attempt_id, replay)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app.state.register_attempt_kind(
        live_cols, qualifies=lambda a, s: s.get("instrument") == "lora"
    )
    app.state.register_attempt_kind(
        live_key_slot, qualifies=lambda a, s: 0 if s.get("instrument") == "lora" else None
    )
    app.state.register_attempt_kind(replay_cols)

    with TestClient(app):
        _, err1 = app.state.start_attempt(
            live_cols,
            "x1",
            600,
            extra={"replay": True, "source": "replay"},
            event_fields={"replay": True, "source": "replay"},
        )
        assert err1 is None
        _, err2 = app.state.start_attempt(
            live_key_slot,
            "spoof-live",
            600,
            event_fields={"replay": True, "source": "replay"},
        )
        assert err2 is None

        now[0] = 4010.0
        app.state.lora.on_reception(_lora_item(from_id="!f5-live"))

        _, err3 = app.state.start_attempt(
            replay_cols,
            "live",
            600,
            extra={"recording": "rf.sigmf", "note": "ok"},
            event_fields={"replay": False, "source": "live"},
        )
        assert err3 is None
        rf_aid = app.state.progress.attempt(replay_cols, "live")["attempt"]["attempt_id"]
        ins, perr = app.state.record_attempt_pass(
            replay_cols,
            "live",
            4010.0,
            0,
            {"passed_at": 4020.0, "recording": "rf.sigmf", "answer": "fm"},
            attempt_id=rf_aid,
            event_fields={"replay": False, "source": "live"},
        )
        assert (ins, perr) == (True, None)

    evts = events.read_events(tmp_path / "events.jsonl")
    for kind_name in ("live_flag_start", "live_flag_pass", "live_ks_start", "live_ks_pass"):
        matched = [e for e in evts if e.get("kind") == kind_name]
        assert len(matched) == 1, kind_name
        assert "replay" not in matched[0]["payload"], (kind_name, matched[0]["payload"])
        assert "source" not in matched[0]["payload"], (kind_name, matched[0]["payload"])

    for kind_name in ("replay_flag_start", "replay_flag_pass"):
        matched = [e for e in evts if e.get("kind") == kind_name]
        assert len(matched) == 1, kind_name
        assert matched[0]["payload"]["replay"] is True, (kind_name, matched[0]["payload"])
        assert matched[0]["payload"]["source"] == "replay", (kind_name, matched[0]["payload"])


def _prepare_managed_dir(tmp_path: Path) -> Path:
    from sigint101.migrations import migrate_store_copy

    gen_dir = tmp_path / "gen"
    gen_dir.mkdir(parents=True, exist_ok=True)
    for store in ("progress", "exam"):
        migrate_store_copy(
            tmp_path / f"missing-{store}.db",
            gen_dir / f"{store}.db",
            store=store,
            allow_missing_source=True,
            scratch_dir=tmp_path / f"scratch-{store}",
        )
    return gen_dir


def _deny_sqlite_action(conn: sqlite3.Connection, action_code: int, arg1_match: str | None = None):
    def auth(action, arg1, arg2, db_name, trigger):
        if action == action_code and (arg1_match is None or arg1 == arg1_match):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(auth)


@pytest.mark.parametrize("fail_at", ["insert", "event", "serialize", "commit"])
@pytest.mark.parametrize("managed", [False, True], ids=["legacy", "managed"])
def test_f3a_failed_start_cleans_up_claimed_receiver_and_preserves_newer_claim(
    tmp_path, monkeypatch, fail_at, managed
):
    """F3a: post-launch Start failures run scoped cleanup after locks and spare newer claims."""
    from sigint101.decode import Decoder
    from tests.child_fakes import Child
    from tests.test_app import _present, _rows, _StubSweeper

    data_dir = _prepare_managed_dir(tmp_path) if managed else tmp_path
    now = [2000.0]
    _patch_log_clock(monkeypatch, now)
    app = app_mod.create_app(
        data_dir=data_dir,
        probe=_present,
        sweeper_factory=lambda: _StubSweeper(_rows()),
        clock=lambda: now[0],
        managed=managed,
    )

    with TestClient(app) as client:
        # 1. Fail Start at INSERT, Start event I/O, journal serialization (TypeError), or COMMIT
        # after launch claims sweeper. Cleanup must always stop sweeper after both locks unwind.
        lock_state: dict[str, bool] = {}

        def launch_sweeper():
            resp = client.get("/api/spectrum")
            assert resp.status_code == 200
            assert app.state.sweeper.running
            return resp

        def check_locks_cleanup():
            # Verify neither DB lock nor reception lock is held during cleanup.
            db_free = app.state.progress._lock.acquire(blocking=False)
            if db_free:
                app.state.progress._lock.release()
            lock_state["db_free"] = db_free
            if fail_at in ("event", "serialize"):
                # Invoking _heard acquires reception_lock -> Progress._lock; would deadlock if held.
                app.state.lora.on_reception(_lora_item(from_id=f"!cleanup-{fail_at}"))
                lock_state["reception_free"] = True

        extra_event_fields = None
        if fail_at == "insert":
            _deny_sqlite_action(app.state.progress._conn, sqlite3.SQLITE_INSERT, "challenges")
        elif fail_at == "commit":
            _deny_sqlite_action(app.state.progress._conn, sqlite3.SQLITE_TRANSACTION, "COMMIT")
        elif fail_at == "serialize":
            # Reach the real journal serializer with a non-JSON value so json.dumps raises.
            extra_event_fields = {"unserializable": object()}
        else:
            real_log = app_mod.log_event

            def fail_start_log(path, kind, payload, **kw):
                if kind == "challenge_start":
                    raise OSError("disk full on start event")
                return real_log(path, kind, payload, **kw)

            monkeypatch.setattr(app_mod, "log_event", fail_start_log)

        try:
            if fail_at == "serialize":
                with pytest.raises(TypeError, match="not JSON serializable"):
                    app.state.start_attempt(
                        challenges.CHALLENGE_KIND,
                        9,
                        900,
                        event_fields=extra_event_fields,
                        launch=launch_sweeper,
                        cleanup=check_locks_cleanup,
                    )
            else:
                _, err = app.state.start_attempt(
                    challenges.CHALLENGE_KIND,
                    9,
                    900,
                    launch=launch_sweeper,
                    cleanup=check_locks_cleanup,
                )
                assert err is not None
        finally:
            app.state.progress._conn.set_authorizer(None)

        assert not app.state.sweeper.running
        assert app.state.progress.challenge(9)["attempt"] is None
        assert lock_state.get("db_free") is True
        if fail_at in ("event", "serialize"):
            assert lock_state.get("reception_free") is True

        # 2a. A newer claim taking over the sweeper AFTER launch returns must keep running.
        def launch_then_superseded():
            resp = client.get("/api/spectrum")
            assert resp.status_code == 200
            assert app.state.sweeper.running
            return resp

        def supersede_and_fail(path, kind, payload, **kw):
            if kind == "challenge_start":
                # Simulate a newer claim taking over sweeper with a new run before cleanup runs.
                app.state.sweeper.stop()
                assert client.get("/api/spectrum").status_code == 200
                raise OSError("event fail after newer claim")
            return events.log_event(path, kind, payload, **kw)

        monkeypatch.setattr(app_mod, "log_event", supersede_and_fail)
        _, err_sup = app.state.start_attempt(
            challenges.CHALLENGE_KIND,
            9,
            900,
            launch=launch_then_superseded,
        )
        assert err_sup is not None
        assert app.state.sweeper.running
        app.state.sweeper.stop()

        # 2b (F3a.2): A newer claim taking over the sweeper DURING the launch hook (after the
        # first spectrum claim returned 200, before the hook returns) must ALSO keep running.
        if fail_at == "event":
            replacement_proc: list = []

            def launch_paused_while_superseded():
                resp = client.get("/api/spectrum")
                assert resp.status_code == 200
                first_proc = app.state.sweeper._proc
                # While this hook is paused, a separate claim stops the first child and launches
                # a replacement fixture child before this hook returns.
                app.state.sweeper.stop()
                assert client.get("/api/spectrum").status_code == 200
                assert app.state.sweeper._proc is not first_proc
                replacement_proc.append(app.state.sweeper._proc)
                return resp

            monkeypatch.setattr(
                app_mod,
                "log_event",
                lambda path, kind, payload, **kw: (
                    (_ for _ in ()).throw(OSError("event fail after mid-hook claim"))
                    if kind == "challenge_start"
                    else events.log_event(path, kind, payload, **kw)
                ),
            )
            _, err_mid = app.state.start_attempt(
                challenges.CHALLENGE_KIND,
                9,
                900,
                launch=launch_paused_while_superseded,
            )
            assert err_mid is not None
            assert app.state.sweeper.running
            assert app.state.sweeper._proc is replacement_proc[0]
            app.state.sweeper.stop()

            # 2c (F3a.2): Explicit LaunchedRun returned by app.state.claim_sdr and explicit
            # HTTP response carrying X-Sigint-Claim each bind cleanup strictly to their own run,
            # even when a separate claim replaces the child during the paused launch hook.
            replacement_proc_2: list = []

            def launch_with_explicit_run():
                run = app.state.claim_sdr("sweeper", app.state.sweeper.start)
                assert run is not None
                assert run.name == "sweeper"
                assert callable(run.stop)
                first_proc = app.state.sweeper._proc
                app.state.sweeper.stop()
                assert client.get("/api/spectrum").status_code == 200
                assert app.state.sweeper._proc is not first_proc
                replacement_proc_2.append(app.state.sweeper._proc)
                return run

            _, err_run = app.state.start_attempt(
                challenges.CHALLENGE_KIND,
                9,
                900,
                launch=launch_with_explicit_run,
            )
            assert err_run is not None
            assert app.state.sweeper.running
            assert app.state.sweeper._proc is replacement_proc_2[0]
            app.state.sweeper.stop()

            replacement_proc_3: list = []

            def launch_returning_response():
                resp = client.get("/api/spectrum")
                assert resp.status_code == 200
                assert "x-sigint-claim" in resp.headers
                first_proc = app.state.sweeper._proc
                app.state.sweeper.stop()
                assert client.get("/api/spectrum").status_code == 200
                assert app.state.sweeper._proc is not first_proc
                replacement_proc_3.append(app.state.sweeper._proc)
                return resp

            _, err_resp = app.state.start_attempt(
                challenges.CHALLENGE_KIND,
                9,
                900,
                launch=launch_returning_response,
            )
            assert err_resp is not None
            assert app.state.sweeper.running
            assert app.state.sweeper._proc is replacement_proc_3[0]
            app.state.sweeper.stop()

    # 3. Unresolved reap on a decoder preserves the lifecycle's failure reason (both OSError and
    # journal serialization TypeError).
    child = Child(["rtl_433"], unkillable=True)
    rtl = Decoder(spawn=lambda *_: child, which=lambda n: n, radio_present=lambda: True)
    data_dir2 = _prepare_managed_dir(tmp_path / "reap") if managed else (tmp_path / "reap")
    data_dir2.mkdir(parents=True, exist_ok=True)
    app2 = app_mod.create_app(
        data_dir=data_dir2,
        probe=_present,
        sweeper_factory=lambda: _StubSweeper(_rows()),
        decoder_factory=lambda: rtl,
        clock=lambda: now[0],
        managed=managed,
    )
    try:
        with TestClient(app2):
            if fail_at == "serialize":
                _patch_log_clock(monkeypatch, now)
                with pytest.raises(TypeError) as exc_info:
                    app2.state.start_attempt(
                        challenges.CHALLENGE_KIND,
                        4,
                        900,
                        event_fields={"bad": object()},
                        launch=rtl.start_owned,
                    )
                msg = str(exc_info.value)
                assert "not JSON serializable" in msg
                assert "rtl_433" in msg
                assert rtl.release_failure(pending=True) is not None
            else:
                monkeypatch.setattr(
                    app_mod,
                    "log_event",
                    lambda *_a, **_kw: (_ for _ in ()).throw(OSError("event fail")),
                )
                _, err_reap = app2.state.start_attempt(
                    challenges.CHALLENGE_KIND,
                    4,
                    900,
                    launch=rtl.start_owned,
                )
                assert err_reap is not None
                assert "rtl_433" in err_reap
                assert rtl.release_failure(pending=True) is not None
            child.unkillable = False
    finally:
        child.unkillable = False
        rtl.stop()


@pytest.mark.parametrize("managed", [False, True], ids=["legacy", "managed"])
def test_f3b_unconfirmed_rollback_quarantines_connection_against_reception_and_next_start(
    tmp_path, monkeypatch, managed
):
    """F3b: quarantined or closed store reads unreadable and never resets passes to READY."""
    data_dir = _prepare_managed_dir(tmp_path) if managed else tmp_path
    now = [3000.0]
    _patch_log_clock(monkeypatch, now)
    app = app_mod.create_app(data_dir=data_dir, clock=lambda: now[0], managed=managed)

    with TestClient(app) as client:
        # 1. Earn a real Tier B pass on Unit 4 first so preexisting progress is on disk.
        assert client.post("/api/challenges/4/start").status_code == 200
        now[0] = 3005.0
        app.state.lora.on_reception(_lora_item(from_id="!f3b-earned-u4"))
        earned = client.get("/api/challenges/4")
        assert earned.status_code == 200
        assert earned.json()["state"] == "passed"
        assert earned.json()["passes"] == 1
        assert "FOUND" in client.get("/unit/4/challenge").text

        # 2. Now fail a Start on Unit 9 with ROLLBACK denied, quarantining the connection.
        real_log = app_mod.log_event

        def fail_start_log(path, kind, payload, **kw):
            if kind == "challenge_start":
                raise OSError("append failed")
            return real_log(path, kind, payload, **kw)

        monkeypatch.setattr(app_mod, "log_event", fail_start_log)
        _deny_sqlite_action(app.state.progress._conn, sqlite3.SQLITE_TRANSACTION, "ROLLBACK")
        r1 = client.post("/api/challenges/9/start")
        assert r1.status_code == 500
        assert r1.json()["state"] == "unreadable"
        assert "quarantined" in (r1.json().get("error") or "").lower()

        # Restore normal log_event; quarantined connection must report unreadable on BOTH units.
        monkeypatch.setattr(app_mod, "log_event", real_log)

        # Unit 4 (which has a preexisting earned pass) MUST read as unreadable, NEVER idle/READY!
        g4 = client.get("/api/challenges/4")
        assert g4.status_code == 503
        g4_body = g4.json()
        assert g4_body["state"] == "unreadable"
        assert g4_body["passes"] is None
        assert g4_body["error"] is not None
        page4 = client.get("/unit/4/challenge")
        assert page4.status_code == 503
        assert "UNREADABLE" in page4.text
        assert "READY" not in page4.text

        # Unit 9 also reads as unreadable (never idle/READY) and must not see the rejected attempt.
        g9 = client.get("/api/challenges/9")
        assert g9.status_code == 503
        g9_body = g9.json()
        assert g9_body["state"] == "unreadable"
        assert g9_body["passes"] is None
        assert g9_body["error"] is not None
        page9 = client.get("/unit/9/challenge")
        assert page9.status_code == 503
        assert "UNREADABLE" in page9.text
        assert "READY" not in page9.text

        # A subsequent qualifying reception must NOT credit the rejected Start or log a pass for 9.
        now[0] = 3010.0
        app.state.lora.on_reception(_lora_item(from_id="!f3b-lora"))

        # Another Start on the quarantined connection must fail and must NOT commit anything.
        now[0] = 3020.0
        r2 = client.post("/api/challenges/9/start")
        assert r2.status_code == 500
        assert r2.json()["state"] == "unreadable"

    # Independent connection confirms Unit 4's earned pass survived intact, and Unit 9 has zero
    # attempts and zero passes.
    check_prog = Progress(data_dir / "progress.db", managed=managed)
    try:
        saved4 = check_prog.challenge(4)
        assert saved4["passes"] == 1
        assert saved4["attempt_pass"] is not None
        saved9 = check_prog.challenge(9)
        assert saved9["attempt"] is None
        assert saved9["passes"] == 0
    finally:
        check_prog.close()
    evts = events.read_events(data_dir / "events.jsonl")
    u9_passes = [
        e for e in evts if e.get("kind") == "challenge_pass" and e["payload"]["unit"] == 9
    ]
    assert u9_passes == []

    # 3. A cleanly closed (non-quarantined) store also reports unreadable, never idle/READY.
    app_closed = app_mod.create_app(data_dir=data_dir, clock=lambda: now[0], managed=managed)
    with TestClient(app_closed) as client_c:
        app_closed.state.progress.close()
        gc = client_c.get("/api/challenges/4")
        assert gc.status_code == 503
        assert gc.json()["state"] == "unreadable"
        assert gc.json()["passes"] is None
        assert gc.json()["error"] is not None
        pc = client_c.get("/unit/4/challenge")
        assert pc.status_code == 503
        assert "UNREADABLE" in pc.text
        assert "READY" not in pc.text


def test_f2a_replay_attempts_bind_durable_identity_and_clip_across_identical_timestamps(
    tmp_path, monkeypatch
):
    """F2a: replay attempts get a never-reused identity bound to recording/digest."""
    now = [2000.0]
    _patch_log_clock(monkeypatch, now)

    # Descriptor construction must require identity_column and recording/digest binding for replay.
    with pytest.raises(ValueError):
        challenges.AttemptKind(
            kind="bad_replay_no_id",
            attempts_table="t_a",
            passes_table="t_p",
            key_column="bench_id",
            slot_column=None,
            start_event="bad_s",
            pass_event="bad_p",
            credit="replay_answer",
            attempt_columns=("recording", "digest"),
            pass_columns=("passed_at", "recording", "digest", "answer"),
        )

    bench = challenges.AttemptKind(
        kind="bench_id_kind",
        attempts_table="test_bench_id_attempts",
        passes_table="test_bench_id_passes",
        key_column="bench_id",
        slot_column=None,
        identity_column="attempt_id",
        start_event="bench_id_start",
        pass_event="bench_id_pass",
        credit="replay_answer",
        attempt_columns=("recording", "digest"),
        pass_columns=("passed_at", "recording", "digest", "answer"),
        label="bench replay",
    )

    prog_init = Progress(tmp_path / "progress.db")
    prog_init.close()
    conn = sqlite3.connect(tmp_path / "progress.db")
    try:
        conn.executescript(
            """
            CREATE TABLE test_bench_id_attempts (
                bench_id   TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                window_s   INTEGER NOT NULL,
                attempt_id TEXT NOT NULL,
                recording  TEXT NOT NULL,
                digest     TEXT NOT NULL
            );
            CREATE TABLE test_bench_id_passes (
                bench_id   TEXT NOT NULL,
                started_at REAL NOT NULL,
                attempt_id TEXT NOT NULL,
                passed_at  REAL NOT NULL,
                recording  TEXT NOT NULL,
                digest     TEXT NOT NULL,
                answer     TEXT NOT NULL,
                PRIMARY KEY (bench_id, attempt_id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app.state.register_attempt_kind(bench)

    with TestClient(app):
        # 1. Start clip A at fixed t=2000.0.
        _, err_a = app.state.start_attempt(
            bench, "b1", 600, extra={"recording": "clip-a.sigmf", "digest": "dig-a"}
        )
        assert err_a is None
        row_a = app.state.progress.attempt(bench, "b1")["attempt"]
        aid_a = row_a["attempt_id"]
        assert isinstance(aid_a, str) and aid_a

        # 2. Failed restart for clip B at IDENTICAL t=2000.0; answer submitted on 2nd connection.
        entered = threading.Event()
        release = threading.Event()
        start_out: dict = {}

        def paused_fail_b():
            entered.set()
            release.wait(timeout=5.0)
            raise RuntimeError("clip B launch aborted")

        t = threading.Thread(
            target=lambda: start_out.update(
                out=app.state.start_attempt(
                    bench,
                    "b1",
                    600,
                    extra={"recording": "clip-b.sigmf", "digest": "dig-b"},
                    launch=paused_fail_b,
                )
            )
        )
        t.start()
        assert entered.wait(timeout=5.0)

        prog2 = Progress(tmp_path / "progress.db")
        try:
            # Even if a caller submits clip B's recording/digest with clip A's attempt_id OR
            # a separate attempt_id at identical t=2000.0, clip A must NOT be credited!
            assert (
                prog2.record_attempt_pass(
                    bench,
                    "b1",
                    2000.0,
                    None,
                    {
                        "passed_at": 2005.0,
                        "recording": "clip-b.sigmf",
                        "digest": "dig-b",
                        "answer": "clip-b-ans",
                    },
                    attempt_id=aid_a,
                )
                is False
            )
            assert (
                prog2.record_attempt_pass(
                    bench,
                    "b1",
                    2000.0,
                    None,
                    {
                        "passed_at": 2005.0,
                        "recording": "clip-b.sigmf",
                        "digest": "dig-b",
                        "answer": "clip-b-ans",
                    },
                    attempt_id="other-id",
                )
                is False
            )
        finally:
            prog2.close()

        release.set()
        t.join(timeout=5.0)
        assert start_out["out"][1] is not None
        assert app.state.progress.attempt(bench, "b1")["passes"] == 0

        # 3. Successful restart for clip B at IDENTICAL t=2000.0 gets a brand-new attempt_id.
        _, err_b = app.state.start_attempt(
            bench, "b1", 600, extra={"recording": "clip-b.sigmf", "digest": "dig-b"}
        )
        assert err_b is None
        row_b = app.state.progress.attempt(bench, "b1")["attempt"]
        aid_b = row_b["attempt_id"]
        assert aid_b != aid_a

        # Stale answer for clip A must be rejected against clip B's row (same started_at!).
        ins_old, err_old = app.state.record_attempt_pass(
            bench,
            "b1",
            2000.0,
            None,
            {"passed_at": 2006.0, "recording": "clip-a.sigmf", "digest": "dig-a", "answer": "a"},
            attempt_id=aid_a,
        )
        assert (ins_old, err_old) == (False, None)
        assert app.state.progress.attempt(bench, "b1")["passes"] == 0

        # Valid answer for clip B succeeds once; duplicate is rejected.
        ins_b, err_pb = app.state.record_attempt_pass(
            bench,
            "b1",
            2000.0,
            None,
            {"passed_at": 2007.0, "recording": "clip-b.sigmf", "digest": "dig-b", "answer": "b"},
            attempt_id=aid_b,
        )
        dup_b, _ = app.state.record_attempt_pass(
            bench,
            "b1",
            2000.0,
            None,
            {"passed_at": 2008.0, "recording": "clip-b.sigmf", "digest": "dig-b", "answer": "b2"},
            attempt_id=aid_b,
        )
        assert (ins_b, err_pb) == (True, None)
        assert dup_b is False
        assert app.state.progress.attempt(bench, "b1")["passes"] == 1

        # 4. Restarting the SAME clip B at identical t=2000.0 produces a fresh attempt_id_c,
        # and an answer carrying attempt_id_b cannot attach to attempt_id_c.
        _, err_c = app.state.start_attempt(
            bench, "b1", 600, extra={"recording": "clip-b.sigmf", "digest": "dig-b"}
        )
        assert err_c is None
        aid_c = app.state.progress.attempt(bench, "b1")["attempt"]["attempt_id"]
        assert aid_c not in (aid_a, aid_b)
        stale_same_clip, _ = app.state.record_attempt_pass(
            bench,
            "b1",
            2000.0,
            None,
            {"passed_at": 2009.0, "recording": "clip-b.sigmf", "digest": "dig-b", "answer": "b3"},
            attempt_id=aid_b,
        )
        assert stale_same_clip is False
        assert app.state.progress.attempt(bench, "b1")["passes"] == 1

    # 5 (F2a): Shared store API — stage clip A, supersede with clip B before A earns credit,
    # reopen the store, and verify A's identity can NEVER be reused via stage_attempt/start_attempt
    # and a later Start gets a strictly monotonic durable identity that rejects A's old answer.
    prog_reopen_1 = Progress(tmp_path / "progress.db")
    try:
        assert prog_reopen_1.stage_attempt(
            bench, "b-reopen", 2000.0, 600,
            extra={"recording": "clip-a.sigmf", "digest": "dig-a"},
        ) is True
        aid_superseded = prog_reopen_1.attempt(bench, "b-reopen")["attempt"]["attempt_id"]
        assert prog_reopen_1.stage_attempt(
            bench, "b-reopen", 2000.0, 600,
            extra={"recording": "clip-b.sigmf", "digest": "dig-b"},
        ) is True
        aid_b_reopen = prog_reopen_1.attempt(bench, "b-reopen")["attempt"]["attempt_id"]
        assert aid_b_reopen > aid_superseded
    finally:
        prog_reopen_1.close()

    prog_reopen_2 = Progress(tmp_path / "progress.db")
    try:
        # Caller-supplied attempt_id (reusing superseded unpassed aid_superseded) MUST be refused.
        with pytest.raises((ValueError, TypeError)):
            prog_reopen_2.stage_attempt(
                bench, "b-reopen", 2000.0, 600,
                extra={"recording": "clip-a.sigmf", "digest": "dig-a"},
                attempt_id=aid_superseded,
            )
        with pytest.raises((ValueError, TypeError)):
            prog_reopen_2.start_attempt(
                bench, "b-reopen", 2000.0, 600,
                attempt_id=aid_superseded,
                recording="clip-a.sigmf",
                digest="dig-a",
            )
        # Store-issued restart for clip A at identical t=2000.0 produces a strictly newer identity.
        assert prog_reopen_2.start_attempt(
            bench, "b-reopen", 2000.0, 600,
            recording="clip-a.sigmf",
            digest="dig-a",
        ) is True
        aid_after_reopen = prog_reopen_2.attempt(bench, "b-reopen")["attempt"]["attempt_id"]
        assert aid_after_reopen > aid_b_reopen
        assert aid_after_reopen != aid_superseded
        # Old answer carrying aid_superseded MUST NOT credit the new attempt.
        assert prog_reopen_2.record_attempt_pass(
            bench, "b-reopen", 2000.0, None,
            {"passed_at": 2010.0, "recording": "clip-a.sigmf", "digest": "dig-a", "answer": "old"},
            attempt_id=aid_superseded,
        ) is False
        assert prog_reopen_2.attempt(bench, "b-reopen")["passes"] == 0
    finally:
        prog_reopen_2.close()


def test_f2b_live_credit_rejects_superseded_attempt_from_second_connection(tmp_path):
    """F2b: live credit refuses to insert a pass if a 2nd connection superseded the attempt."""
    db_path = tmp_path / "progress.db"
    slotted = _slotted_live_kind()
    prog1 = Progress(db_path)
    _create_slotted_tables(db_path)
    prog2 = Progress(db_path)
    try:
        # 1a. Tier B (single-slot CHALLENGE_KIND) — distinct timestamp replacement.
        assert prog1.start_challenge(9, 1000.0, 900) is True
        superseded_tier_b = [False]

        def tier_b_qualifies(attempt, stamp):
            if not superseded_tier_b[0]:
                superseded_tier_b[0] = True
                # Second connection commits a superseding Start right after candidate SELECT!
                assert prog2.start_challenge(9, 1100.0, 900) is True
            return True

        stamp = _make_stamp("lora", 1010.0, "d-f2b-1", event_id="ev-f2b-1")
        credited_b = prog1.credit_challenges([stamp], tier_b_qualifies)
        assert credited_b == []
        assert prog2.challenge(9)["passes"] == 0
        assert prog2.challenge(9)["attempt"]["started_at"] == 1100.0

        # 1b. Tier B — SAME timestamp replacement with a shorter window (F2b round 4).
        # Evidence at t=1150 qualifies under the old 900 s window, but NOT the replacement 10 s
        # window; guarded insert must compare durable attempt identity, not just started_at.
        superseded_same_ts_b = [False]

        def tier_b_same_ts_qualifies(attempt, s):
            if not superseded_same_ts_b[0]:
                superseded_same_ts_b[0] = True
                assert prog2.start_challenge(9, 1100.0, 10) is True
            return challenges.in_window(attempt, s)

        stamp_late = _make_stamp("lora", 1150.0, "d-f2b-same-ts", event_id="ev-f2b-same-ts")
        assert prog1.credit_challenges([stamp_late], tier_b_same_ts_qualifies) == []
        assert prog2.challenge(9)["passes"] == 0
        assert prog2.challenge(9)["attempt"]["window_s"] == 10

        # Normal credit against the current (1100.0, 10 s) attempt still inserts once.
        stamp2 = _make_stamp("lora", 1105.0, "d-f2b-2", event_id="ev-f2b-2")
        assert len(prog1.credit_challenges([stamp2], lambda a, s: challenges.in_window(a, s))) == 1
        assert prog1.credit_challenges([stamp2], lambda a, s: challenges.in_window(a, s)) == []
        assert prog2.challenge(9)["passes"] == 1

        # 2a. Slotted live_reception kind — distinct timestamp replacement.
        assert prog1.start_attempt(slotted, "hunt-r", 1000.0, 900) is True
        superseded_slot = [False]

        def slot_qualifies(attempt, s):
            if not superseded_slot[0]:
                superseded_slot[0] = True
                assert prog2.start_attempt(slotted, "hunt-r", 1100.0, 900) is True
            return 0

        credited_s = prog1.credit_attempts(slotted, [stamp], slot_qualifies)
        assert credited_s == []
        assert prog2.attempt(slotted, "hunt-r")["passes"] == 0
        assert prog2.attempt(slotted, "hunt-r")["attempt"]["started_at"] == 1100.0

        # 2b. Slotted live_reception kind — SAME timestamp replacement with a shorter window.
        superseded_same_ts_slot = [False]

        def slot_same_ts_qualifies(attempt, s):
            if not superseded_same_ts_slot[0]:
                superseded_same_ts_slot[0] = True
                assert prog2.start_attempt(slotted, "hunt-r", 1100.0, 10) is True
            return 0 if challenges.in_window(attempt, s) else None

        assert prog1.credit_attempts(slotted, [stamp_late], slot_same_ts_qualifies) == []
        assert prog2.attempt(slotted, "hunt-r")["passes"] == 0
        assert prog2.attempt(slotted, "hunt-r")["attempt"]["window_s"] == 10

        # Normal slotted credit against current (1100.0, 10 s) attempt inserts once per slot.
        assert len(
            prog1.credit_attempts(
                slotted, [stamp2], lambda a, s: 0 if challenges.in_window(a, s) else None
            )
        ) == 1
        assert prog1.credit_attempts(
            slotted, [stamp2], lambda a, s: 0 if challenges.in_window(a, s) else None
        ) == []
        assert prog2.attempt(slotted, "hunt-r")["passes"] == 1
    finally:
        prog1.close()
        prog2.close()


def test_f5_reserved_event_key_aliases_rejected_and_safe_alias_preserves_backfill_anchor(
    tmp_path, monkeypatch
):
    """F5: reserved event keys are rejected at build; safe alias preserves backfill anchor."""
    for bad_kwargs in (
        {"key_column": "hunt_id", "event_key_field": "source"},
        {"key_column": "hunt_id", "event_key_field": "replay"},
        {"key_column": "source", "event_key_field": ""},
        {"key_column": "replay", "event_key_field": ""},
    ):
        with pytest.raises(ValueError):
            challenges.AttemptKind(
                kind="bad_key",
                attempts_table="t_a",
                passes_table="t_p",
                slot_column=None,
                start_event="bad_s",
                pass_event="bad_p",
                credit="live_reception",
                **bad_kwargs,
            )

    # A reserved SQL key_column WITH an explicit safe event_key_field works and anchors backfill.
    now = [2000.0]
    _patch_log_clock(monkeypatch, now)
    safe_kind = challenges.AttemptKind(
        kind="safe_alias_live",
        attempts_table="test_safe_alias_attempts",
        passes_table="test_safe_alias_passes",
        key_column="source",
        slot_column=None,
        start_event="safe_alias_start",
        pass_event="safe_alias_pass",
        credit="live_reception",
        event_key_field="attempt_key",
    )

    prog_init = Progress(tmp_path / "progress.db")
    prog_init.close()
    conn = sqlite3.connect(tmp_path / "progress.db")
    try:
        conn.executescript(
            """
            CREATE TABLE test_safe_alias_attempts (
                source     TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                window_s   INTEGER NOT NULL
            );
            CREATE TABLE test_safe_alias_passes (
                source     TEXT NOT NULL,
                started_at REAL NOT NULL,
                passed_at  REAL NOT NULL,
                instrument TEXT NOT NULL,
                provenance TEXT NOT NULL,
                digest     TEXT NOT NULL,
                PRIMARY KEY (source, started_at)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    def qualifies(attempt, stamp):
        return stamp.get("instrument") == "lora" and challenges.in_window(attempt, stamp)

    app = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app.state.register_attempt_kind(safe_kind, qualifies=qualifies)
    with TestClient(app):
        # 1. Pre-Start reception at t=2000.0.
        app.state.lora.on_reception(_lora_item(from_id="!f5-pre"))
        # 2. Clock steps backward to t=1500.0; start attempt.
        now[0] = 1500.0
        _, err = app.state.start_attempt(
            safe_kind,
            "k1",
            900,
            event_fields={"replay": True, "source": "replay"},
        )
        assert err is None

    # 3. Restart -> backfill must map safe_alias_start via attempt_key and reject pre-Start rx!
    app2 = app_mod.create_app(data_dir=tmp_path, clock=lambda: now[0])
    app2.state.register_attempt_kind(safe_kind, qualifies=qualifies)
    with TestClient(app2):
        assert app2.state.progress.attempt(safe_kind, "k1")["passes"] == 0
        # Post-Start reception credits cleanly with reserved flags stripped.
        now[0] = 1510.0
        app2.state.lora.on_reception(_lora_item(from_id="!f5-post"))
        assert app2.state.progress.attempt(safe_kind, "k1")["passes"] == 1

    evts = events.read_events(tmp_path / "events.jsonl")
    starts = [e for e in evts if e.get("kind") == "safe_alias_start"]
    passes = [e for e in evts if e.get("kind") == "safe_alias_pass"]
    assert len(starts) == 1
    assert starts[0]["payload"]["attempt_key"] == "k1"
    assert "source" not in starts[0]["payload"] and "replay" not in starts[0]["payload"]
    assert len(passes) == 1
    assert passes[0]["payload"]["attempt_key"] == "k1"
    assert "source" not in passes[0]["payload"] and "replay" not in passes[0]["payload"]


# --- Round 5: Owned-run handle contract, atomic compare-and-stop, pre-launch cleanup boundary ---


def _make_live_child_cls():
    from tests.child_fakes import Child

    class _LivePipe:
        def __init__(self, closed: threading.Event):
            self._closed = closed

        def __iter__(self):
            self._closed.wait(timeout=15.0)
            return iter(())

        def close(self):
            self._closed.set()

    class _LiveChild(Child):
        """Fake streaming child whose stdout stays open until terminated, killed, or closed."""

        def __init__(self, args=("tool",)):
            super().__init__(args)
            self._closed = threading.Event()
            self.stdout = _LivePipe(self._closed)

        def terminate(self):
            super().terminate()
            self._closed.set()

        def kill(self):
            super().kill()
            self._closed.set()

    return _LiveChild


def test_r5_interleaving_unrelated_claim_before_launch_and_reversed_replacement(
    tmp_path, monkeypatch
):
    """R5 (codex2 interleavings): exact OwnedRun handle tracks only the run acquired by the hook;
    no global claim-window or receiver-snapshot fallback can steal or mis-target another claim."""
    from sigint101.decode import Decoder
    from sigint101.hardware import RtlSdrDevice
    from sigint101.spectrum import Sweeper
    from tests.test_app import _present

    LiveChild = _make_live_child_cls()
    now = [5000.0]
    _patch_log_clock(monkeypatch, now)
    pwr_children: list = []
    rtl_children: list = []

    def spawn_pwr(cmd, **_kw):
        ch = LiveChild(cmd)
        pwr_children.append(ch)
        return ch

    def spawn_rtl(cmd, **_kw):
        ch = LiveChild(cmd)
        rtl_children.append(ch)
        return ch

    devs = [RtlSdrDevice(0, "rx0")]
    sweeper = Sweeper(
        spawn=spawn_pwr, radio_present=lambda: True, list_devices=lambda: devs,
    )
    rtl = Decoder(
        spawn=spawn_rtl, which=lambda n: n, radio_present=lambda: True, list_devices=lambda: devs,
    )
    app = app_mod.create_app(
        data_dir=tmp_path,
        probe=_present,
        sweeper_factory=lambda: sweeper,
        decoder_factory=lambda: rtl,
        clock=lambda: now[0],
    )

    real_log = app_mod.log_event

    def fail_start_log(path, kind, payload, **kw):
        if kind == "challenge_start":
            raise OSError("append failed")
        return real_log(path, kind, payload, **kw)

    monkeypatch.setattr(app_mod, "log_event", fail_start_log)

    try:
        with TestClient(app) as client:
            # Interleaving A: pause Start hook BEFORE its claim; an unrelated request opens rtl_433;
            # hook then claims rtl_power via /api/spectrum and returns its response receipt.
            # When journal append fails, rtl_power MUST be stopped and no attempt committed.
            paused = threading.Event()
            resume = threading.Event()
            holder: dict = {}

            def launch_after_unrelated():
                paused.set()
                assert resume.wait(timeout=5.0)
                resp = client.get("/api/spectrum")
                assert resp.status_code == 200
                assert "x-sigint-claim" in resp.headers
                return resp

            t = threading.Thread(
                target=lambda: holder.update(
                    out=app.state.start_attempt(
                        challenges.CHALLENGE_KIND, 9, 900, launch=launch_after_unrelated,
                    )
                )
            )
            t.start()
            assert paused.wait(timeout=5.0)
            assert client.get("/api/decode").status_code == 200
            assert len(rtl_children) == 1 and rtl_children[-1].poll() is None
            resume.set()
            t.join(timeout=5.0)

            assert holder["out"][1] is not None and "append failed" in holder["out"][1]
            assert not t.is_alive()
            assert len(pwr_children) == 1
            assert pwr_children[-1].poll() is not None, "failed Start left rtl_power running"
            assert not sweeper.running
            assert app.state.progress.challenge(9)["attempt"] is None

            # Interleaving B (reversed): hook opens rtl_433 directly via start_owned(), another
            # request claims rtl_power (standing rtl_433 down), then Start fails on journal append.
            # Cleanup must NOT stop the newer rtl_power child!
            rtl_children.clear()
            pwr_children.clear()
            rtl.resume()

            def launch_rtl_then_replaced():
                handle = rtl.start_owned()
                assert handle is not None
                assert len(rtl_children) == 1 and rtl_children[-1].poll() is None
                assert client.get("/api/spectrum").status_code == 200
                assert len(pwr_children) == 1 and pwr_children[-1].poll() is None
                return handle

            _, err_b = app.state.start_attempt(
                challenges.CHALLENGE_KIND, 9, 900, launch=launch_rtl_then_replaced,
            )
            assert err_b is not None and "append failed" in err_b
            assert len(pwr_children) == 1
            assert pwr_children[-1].poll() is None, "cleanup killed replacement rtl_power"
            assert sweeper.running
            sweeper.stop()

            # Fallback-deleted check: an unrelated thread starts rtl_power during a hook that
            # itself acquired and returned NO handle. Start failure MUST NOT touch it!
            pwr_children.clear()
            paused2 = threading.Event()
            resume2 = threading.Event()
            holder2: dict = {}

            def noop_hook_paused():
                paused2.set()
                assert resume2.wait(timeout=5.0)
                return None

            t2 = threading.Thread(
                target=lambda: holder2.update(
                    out=app.state.start_attempt(
                        challenges.CHALLENGE_KIND, 9, 900, launch=noop_hook_paused,
                    )
                )
            )
            t2.start()
            assert paused2.wait(timeout=5.0)
            assert client.get("/api/spectrum").status_code == 200
            assert len(pwr_children) == 1 and pwr_children[-1].poll() is None
            resume2.set()
            t2.join(timeout=5.0)
            assert not t2.is_alive()

            assert holder2["out"][1] is not None and "append failed" in holder2["out"][1]
            assert pwr_children[-1].poll() is None, (
                "deleted global claim-window fallback must not stop an unrelated thread's receiver"
            )
            assert sweeper.running
            sweeper.stop()
    finally:
        sweeper.stop()
        rtl.stop()


def test_r5_overlapping_claims_on_only_receiver_vs_genuine_replacement_and_borrowed_claim(
    tmp_path, monkeypatch
):
    """R5: overlapping claims on the only running receiver do not invalidate the owner's handle
    (borrowed claims get no handle), while a genuine replacement is always spared."""
    from sigint101.acars import AcarsDecoder
    from sigint101.hardware import RtlSdrDevice
    from sigint101.spectrum import Sweeper
    from tests.test_app import _present

    LiveChild = _make_live_child_cls()
    now = [6000.0]
    _patch_log_clock(monkeypatch, now)
    pwr_children: list = []
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    pause_next_spawn = [False]

    def spawn_pwr(cmd, **_kw):
        ch = LiveChild(cmd)
        pwr_children.append(ch)
        if pause_next_spawn[0]:
            pause_next_spawn[0] = False
            spawn_entered.set()
            assert release_spawn.wait(timeout=5.0)
        return ch

    acars_children: list = []

    def spawn_acars(*_a, **_kw):
        ch = LiveChild(["acarsdec"])
        acars_children.append(ch)
        return ch

    devs = [RtlSdrDevice(0, "rx0")]
    sweeper = Sweeper(
        spawn=spawn_pwr, radio_present=lambda: True, list_devices=lambda: devs,
    )
    acars = AcarsDecoder(
        spawn=spawn_acars, which=lambda n: n, radio_present=lambda: True, list_devices=lambda: devs,
    )
    app = app_mod.create_app(
        data_dir=tmp_path,
        probe=_present,
        sweeper_factory=lambda: sweeper,
        acars_factory=lambda: acars,
        clock=lambda: now[0],
    )

    real_log = app_mod.log_event
    monkeypatch.setattr(
        app_mod,
        "log_event",
        lambda path, kind, payload, **kw: (
            (_ for _ in ()).throw(OSError("append failed"))
            if kind == "challenge_start"
            else real_log(path, kind, payload, **kw)
        ),
    )

    try:
        with TestClient(app) as client:
            # 1. Two overlapping HTTP /api/spectrum claims on the only receiver (1 spawn total).
            pause_next_spawn[0] = True
            spawn_entered.clear()
            release_spawn.clear()
            r1_box: dict = {}
            r2_box: dict = {}

            def req1():
                r1_box["resp"] = client.get("/api/spectrum")

            def req2():
                r2_box["resp"] = client.get("/api/spectrum")

            def launch_overlapping_http():
                t1 = threading.Thread(target=req1)
                t2 = threading.Thread(target=req2)
                t1.start()
                try:
                    assert spawn_entered.wait(timeout=5.0)
                    t2.start()
                    time.sleep(0.05)  # Let req2 enter the arbiter reservation queue.
                finally:
                    release_spawn.set()
                    t1.join(timeout=5.0)
                    if t2.ident is not None:
                        t2.join(timeout=5.0)
                assert not t1.is_alive() and not t2.is_alive()
                assert r1_box["resp"].status_code == 200
                assert r2_box["resp"].status_code == 200
                assert "x-sigint-claim" in r1_box["resp"].headers
                assert "x-sigint-claim" not in r2_box["resp"].headers
                return r1_box["resp"]

            _, err1 = app.state.start_attempt(
                challenges.CHALLENGE_KIND, 9, 900, launch=launch_overlapping_http,
            )
            assert err1 is not None and "append failed" in err1
            assert len(pwr_children) == 1
            assert pwr_children[0].poll() is not None, (
                "second overlapping claim invalidated owner's handle and left rtl_power running"
            )
            assert not sweeper.running

            # 2. Two overlapping direct app.state.claim_sdr calls on the only receiver.
            pwr_children.clear()
            pause_next_spawn[0] = True
            spawn_entered.clear()
            release_spawn.clear()
            c1_box: dict = {}
            c2_box: dict = {}

            def launch_overlapping_direct():
                t1 = threading.Thread(
                    target=lambda: c1_box.update(
                        h=app.state.claim_sdr("sweeper", sweeper.start)
                    )
                )
                t2 = threading.Thread(
                    target=lambda: c2_box.update(
                        h=app.state.claim_sdr("sweeper", sweeper.start)
                    )
                )
                t1.start()
                try:
                    assert spawn_entered.wait(timeout=5.0)
                    t2.start()
                    time.sleep(0.05)
                finally:
                    release_spawn.set()
                    t1.join(timeout=5.0)
                    if t2.ident is not None:
                        t2.join(timeout=5.0)
                assert not t1.is_alive() and not t2.is_alive()
                assert c1_box["h"] is not None
                assert c2_box["h"] is None, "borrowed claim must return no handle"
                return c1_box["h"]

            _, err2 = app.state.start_attempt(
                challenges.CHALLENGE_KIND, 9, 900, launch=launch_overlapping_direct,
            )
            assert err2 is not None and "append failed" in err2
            assert len(pwr_children) == 1
            assert pwr_children[0].poll() is not None
            assert not sweeper.running

            # 3. Genuine replacement: stop first child and spawn a second -> cleanup spares child 2.
            pwr_children.clear()

            def launch_only_h1():
                h1 = app.state.claim_sdr("sweeper", sweeper.start)
                assert h1 is not None
                return h1

            def replace_on_log(path, kind, payload, **kw):
                if kind == "challenge_start":
                    sweeper.stop()
                    h2 = app.state.claim_sdr("sweeper", sweeper.start)
                    assert h2 is not None
                    raise OSError("append failed after genuine replacement")
                return real_log(path, kind, payload, **kw)

            monkeypatch.setattr(app_mod, "log_event", replace_on_log)
            _, err3 = app.state.start_attempt(
                challenges.CHALLENGE_KIND, 9, 900, launch=launch_only_h1,
            )
            assert err3 is not None
            assert len(pwr_children) == 2
            assert pwr_children[0].poll() is not None
            assert pwr_children[1].poll() is None, "cleanup stopped genuine replacement child"
            assert sweeper.running

            # 4. Borrowed already-running receiver: claim returns None, Start failure leaves it up.
            monkeypatch.setattr(
                app_mod,
                "log_event",
                lambda path, kind, payload, **kw: (
                    (_ for _ in ()).throw(OSError("append failed on borrowed"))
                    if kind == "challenge_start"
                    else real_log(path, kind, payload, **kw)
                ),
            )
            borrowed_handles: list = []

            def launch_borrowed():
                h = app.state.claim_sdr("sweeper", sweeper.start)
                borrowed_handles.append(h)
                return h

            _, err4 = app.state.start_attempt(
                challenges.CHALLENGE_KIND, 9, 900, launch=launch_borrowed,
            )
            assert err4 is not None
            assert borrowed_handles == [None]
            assert pwr_children[1].poll() is None, "failed Start stopped borrowed receiver"
            assert sweeper.running
            sweeper.stop()

            # 5. SupervisedReceiver (AcarsDecoder): overlapping claim borrows (None), owner stops.
            def launch_acars_with_borrow():
                h_own = app.state.claim_sdr("acars")
                assert h_own is not None
                h_borrow = app.state.claim_sdr("acars")
                assert h_borrow is None
                return h_own

            _, err5 = app.state.start_attempt(
                challenges.CHALLENGE_KIND, 9, 900, launch=launch_acars_with_borrow,
            )
            assert err5 is not None and "append failed" in err5
            assert len(acars_children) == 1
            assert acars_children[0].poll() is not None
            assert not acars.running
    finally:
        release_spawn.set()
        sweeper.stop()
        acars.shutdown()


def test_r5_launch_hook_exception_after_successful_claim_stops_owned_run(tmp_path, monkeypatch):
    """R5: if a launch hook claims/starts a receiver and then raises before returning, the
    pre-installed cleanup boundary stops the registered owned run after locks unwind."""
    from sigint101.decode import Decoder
    from sigint101.hardware import RtlSdrDevice
    from sigint101.spectrum import Sweeper
    from tests.test_app import _present

    LiveChild = _make_live_child_cls()
    now = [7000.0]
    _patch_log_clock(monkeypatch, now)
    pwr_children: list = []
    rtl_children: list = []
    devs = [RtlSdrDevice(0, "rx0")]
    sweeper = Sweeper(
        spawn=lambda cmd, **_kw: (pwr_children.append(LiveChild(cmd)) or pwr_children[-1]),
        radio_present=lambda: True,
        list_devices=lambda: devs,
    )
    rtl = Decoder(
        spawn=lambda cmd, **_kw: (rtl_children.append(LiveChild(cmd)) or rtl_children[-1]),
        which=lambda n: n,
        radio_present=lambda: True,
        list_devices=lambda: devs,
    )
    app = app_mod.create_app(
        data_dir=tmp_path,
        probe=_present,
        sweeper_factory=lambda: sweeper,
        decoder_factory=lambda: rtl,
        clock=lambda: now[0],
    )

    try:
        with TestClient(app):
            # Case 1: hook calls app.state.claim_sdr successfully, then raises OSError.
            def claim_then_raise():
                h = app.state.claim_sdr("sweeper", sweeper.start)
                assert h is not None
                assert pwr_children[-1].poll() is None
                raise OSError("post-claim hook I/O failure")

            _, err1 = app.state.start_attempt(
                challenges.CHALLENGE_KIND, 9, 900, launch=claim_then_raise,
            )
            assert err1 is not None and "post-claim hook I/O failure" in err1
            assert len(pwr_children) == 1
            assert pwr_children[0].poll() is not None, (
                "hook exception after claim_sdr bypassed cleanup and left child running"
            )
            assert not sweeper.running
            assert app.state.progress.challenge(9)["attempt"] is None

            # Case 2: hook calls rtl.start_owned() directly, then raises RuntimeError.
            rtl.resume()

            def start_owned_then_raise():
                h = rtl.start_owned()
                assert h is not None
                assert rtl_children[-1].poll() is None
                raise RuntimeError("hook crashed after start_owned")

            _, err2 = app.state.start_attempt(
                challenges.CHALLENGE_KIND, 9, 900, launch=start_owned_then_raise,
            )
            assert err2 is not None and "hook crashed after start_owned" in err2
            assert len(rtl_children) == 1
            assert rtl_children[0].poll() is not None, (
                "hook exception after start_owned bypassed cleanup and left rtl_433 running"
            )
            assert not rtl.running
            assert app.state.progress.challenge(9)["attempt"] is None
    finally:
        sweeper.stop()
        rtl.stop()


def test_r5_clock_exception_after_launch_stops_owned_run_and_preserves_exception(
    tmp_path, monkeypatch
):
    """R5: if the injected clock raises after launch returns an owned handle, cleanup stops the
    run after both locks unwind and the original exception is preserved."""
    from sigint101.hardware import RtlSdrDevice
    from sigint101.spectrum import Sweeper
    from tests.test_app import _present

    LiveChild = _make_live_child_cls()
    pwr_children: list = []
    devs = [RtlSdrDevice(0, "rx0")]
    sweeper = Sweeper(
        spawn=lambda cmd, **_kw: (pwr_children.append(LiveChild(cmd)) or pwr_children[-1]),
        radio_present=lambda: True,
        list_devices=lambda: devs,
    )
    boom_clock = [False]

    def flaky_clock():
        if boom_clock[0]:
            raise TypeError("clock failure after launch")
        return 8000.0

    app = app_mod.create_app(
        data_dir=tmp_path,
        probe=_present,
        sweeper_factory=lambda: sweeper,
        clock=flaky_clock,
    )

    try:
        with TestClient(app):
            lock_state: dict[str, bool] = {}

            def check_locks_free():
                db_free = app.state.progress._lock.acquire(blocking=False)
                if db_free:
                    app.state.progress._lock.release()
                lock_state["db_free"] = db_free
                app.state.lora.on_reception(_lora_item(from_id="!r5-clock-cleanup"))
                lock_state["reception_free"] = True

            def launch_and_arm_clock_fault():
                h = app.state.claim_sdr("sweeper", sweeper.start)
                assert h is not None
                boom_clock[0] = True
                return h

            with pytest.raises(TypeError, match="clock failure after launch"):
                app.state.start_attempt(
                    challenges.CHALLENGE_KIND,
                    9,
                    900,
                    launch=launch_and_arm_clock_fault,
                    cleanup=check_locks_free,
                )

            boom_clock[0] = False
            assert len(pwr_children) == 1
            assert pwr_children[0].poll() is not None, (
                "clock TypeError after launch escaped cleanup and left rtl_power running"
            )
            assert not sweeper.running
            assert app.state.progress.challenge(9)["attempt"] is None
            assert lock_state == {"db_free": True, "reception_free": True}

            # Also verify an unsupported non-handle return value raises TypeError inside the
            # cleanup boundary and still stops any run already registered during launch().
            pwr_children.clear()

            def launch_bad_return():
                h = app.state.claim_sdr("sweeper", sweeper.start)
                assert h is not None
                return "not-an-owned-run-handle"

            with pytest.raises(TypeError, match="unsupported handle"):
                app.state.start_attempt(
                    challenges.CHALLENGE_KIND, 9, 900, launch=launch_bad_return,
                )
            assert len(pwr_children) == 1
            assert pwr_children[0].poll() is not None
            assert not sweeper.running
    finally:
        boom_clock[0] = False
        sweeper.stop()
