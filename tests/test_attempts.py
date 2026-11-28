# ABOUTME: Tests for Slice 1a-i's shared attempt layer (AttemptKind, Progress, app hooks).
# ABOUTME: Uses test-only attempt kinds and local tables to prove live vs replay separation.

from __future__ import annotations

import json
import sqlite3
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
                recording  TEXT NOT NULL,
                digest     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS test_bench_passes (
                bench_id   TEXT NOT NULL,
                started_at REAL NOT NULL,
                passed_at  REAL NOT NULL,
                recording  TEXT NOT NULL,
                digest     TEXT NOT NULL,
                answer     TEXT NOT NULL,
                PRIMARY KEY (bench_id, started_at)
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
        assert first["attempt"] == {
            "bench_id": "b1",
            "started_at": 1000.0,
            "window_s": 600,
            "recording": "clip-1.sigmf",
            "digest": "d1",
        }

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
        assert prog.record_attempt_pass(bench, "b1", 1000.0, None, pass_one) is True
        assert prog.record_attempt_pass(bench, "b1", 1000.0, None, pass_two) is False

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
