# ABOUTME: Tests PR 1 of the updater plan: schema fixtures, numbered migrations, managed open,
# ABOUTME: durability/acknowledgement fences, live SQLite PRAGMAs, and R2-1/R2-4 build rules.

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101 import events as events_mod
from sigint101 import sstv as sstv_mod
from sigint101 import storage as storage_mod
from sigint101.app import create_app
from sigint101.apt import AptDecoder
from sigint101.exam import ExamStore
from sigint101.hardware import RadioProbe
from sigint101.migrations import (
    EXAM_SCHEMA_VERSION,
    PROGRESS_SCHEMA_VERSION,
    _snapshot_rows,
    _verify_preserved_rows,
    match_v0_signature,
    migrate_store_copy,
    reviewed_signatures,
)
from sigint101.progress import Progress
from sigint101.sstv import SstvDecoder, _Capture
from sigint101.storage import (
    DEFAULT_UPDATER_ROOT_REL,
    FORBIDDEN_ROOT_REL,
    SchemaError,
    open_managed_sqlite,
    schema_signature,
    validate_updater_root,
    verify_managed_connection,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _absent():
    return RadioProbe(False, "lsusb", "no RTL2832U on USB bus")


# --- Historical v0 builders matching main's real git history -----------------------------------


_REPLAY_PROGRESS_BASES = {
    "v0_replays_seen": "v0_1a35a1a",
    "v0_retrieval_replays_seen": "v0_retrieval",
    "v0_challenges_retrieval_replays_seen": "v0_challenges_retrieval",
}


def _build_progress_v0(path: Path, variant: str) -> dict:
    """Create a real historical version-0 progress.db fixture and return seeded expected rows."""
    with_solos = variant == "v0_solos"
    with_replay = with_solos or variant in _REPLAY_PROGRESS_BASES
    variant = (
        "v0_challenges_retrieval"
        if with_solos
        else _REPLAY_PROGRESS_BASES.get(variant, variant)
    )
    conn = sqlite3.connect(path)
    expected: dict = {}
    if variant == "empty":
        conn.commit()
        conn.close()
        return expected

    conn.executescript("""
        CREATE TABLE progress (
            lesson_id INTEGER PRIMARY KEY,
            opened    INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            updated   REAL
        );
        CREATE TABLE responses (
            lesson_id INTEGER NOT NULL,
            kind      TEXT NOT NULL,
            body      TEXT NOT NULL,
            updated   REAL,
            PRIMARY KEY (lesson_id, kind)
        );
    """)
    conn.execute(
        "INSERT INTO progress(lesson_id, opened, completed, updated) VALUES(1, 1, 1, 100.5)"
    )
    conn.execute(
        "INSERT INTO responses(lesson_id, kind, body, updated) VALUES(1, 'prediction', 'a', 100.0)"
    )
    expected["progress"] = [(1, 1, 1, 100.5)]
    expected["responses"] = [(1, "prediction", "a", 100.0, None)]

    if variant == "v0_72ddf9c":
        conn.commit()
        conn.close()
        return expected

    conn.executescript("""
        CREATE TABLE stages (
            lesson_id INTEGER PRIMARY KEY,
            stage     TEXT NOT NULL,
            furthest  INTEGER NOT NULL DEFAULT 0,
            updated   REAL
        );
        CREATE TABLE places (
            name    TEXT PRIMARY KEY,
            value   TEXT NOT NULL,
            updated REAL
        );
    """)
    conn.execute("ALTER TABLE responses ADD COLUMN correct INTEGER")
    conn.execute(
        "INSERT INTO responses(lesson_id, kind, body, updated, correct) "
        "VALUES(2, 'prediction', 'b', 101.0, 1)"
    )
    conn.execute(
        "INSERT INTO stages(lesson_id, stage, furthest, updated) VALUES(1, 'explain', 5, 102.0)"
    )
    conn.execute(
        "INSERT INTO places(name, value, updated) VALUES('rack', 'adsb', 103.0)"
    )
    expected["responses"].append((2, "prediction", "b", 101.0, 1))
    expected["stages"] = [(1, "explain", 5, 102.0)]
    expected["places"] = [("rack", "adsb", 103.0)]

    if variant == "v0_02c2095":
        conn.commit()
        conn.close()
        return expected

    conn.executescript("""
        CREATE TABLE stamps (
            instrument TEXT PRIMARY KEY,
            first_ts   REAL NOT NULL,
            count      INTEGER NOT NULL,
            provenance TEXT NOT NULL,
            digest     TEXT NOT NULL
        );
        CREATE TABLE stamp_events (
            digest BLOB PRIMARY KEY
        ) WITHOUT ROWID;
    """)
    prov = json.dumps({"instrument": "ism", "tool": "rtl_433"})
    digest_hex = "ab" * 32
    watermark = bytes.fromhex(digest_hex)
    conn.execute(
        "INSERT INTO stamps(instrument, first_ts, count, provenance, digest) "
        "VALUES('ism', 1700000000.5, 3, ?, ?)",
        (prov, digest_hex),
    )
    conn.execute("INSERT INTO stamp_events(digest) VALUES(?)", (watermark,))
    expected["stamps"] = [("ism", 1700000000.5, 3, prov, digest_hex)]
    expected["stamp_events"] = [(watermark,)]

    if variant == "v0_e72c706":
        conn.commit()
        conn.close()
        return expected

    conn.executescript("""
        CREATE TABLE sort_answers (
            lesson_id INTEGER NOT NULL,
            card_key  TEXT NOT NULL,
            choice    TEXT NOT NULL,
            correct   INTEGER NOT NULL,
            updated   REAL,
            PRIMARY KEY (lesson_id, card_key)
        );
    """)
    conn.execute(
        "INSERT INTO sort_answers(lesson_id, card_key, choice, correct, updated) "
        "VALUES(5, 'aircraft-am', 'legal', 1, 104.0)"
    )
    expected["sort_answers"] = [(5, "aircraft-am", "legal", 1, 104.0)]
    if variant == "v0_1a35a1a" and not with_replay:
        conn.commit()
        conn.close()
        return expected

    assert variant in ("v0_1a35a1a", "v0_challenges", "v0_retrieval",
                       "v0_challenges_retrieval")
    if variant in ("v0_retrieval", "v0_challenges_retrieval"):
        conn.executescript("""
            CREATE TABLE retrieval (
                lesson_id   INTEGER PRIMARY KEY,
                box         INTEGER NOT NULL DEFAULT 1,
                last_seen   REAL NOT NULL,
                seen        INTEGER NOT NULL DEFAULT 0,
                correct     INTEGER NOT NULL DEFAULT 0,
                last_choice TEXT
            );
        """)
        conn.execute(
            "INSERT INTO retrieval(lesson_id, box, last_seen, seen, correct, last_choice) "
            "VALUES(1, 2, 105.0, 3, 2, 'a')"
        )
        expected["retrieval"] = [(1, 2, 105.0, 3, 2, "a")]
    if variant in ("v0_challenges", "v0_challenges_retrieval"):
        conn.executescript("""
            CREATE TABLE challenges (
                unit_id    INTEGER PRIMARY KEY,
                started_at REAL NOT NULL,
                window_s   INTEGER NOT NULL
            );
            CREATE TABLE challenge_passes (
                unit_id    INTEGER NOT NULL,
                started_at REAL NOT NULL,
                passed_at  REAL NOT NULL,
                instrument TEXT NOT NULL,
                provenance TEXT NOT NULL,
                digest     TEXT NOT NULL,
                PRIMARY KEY (unit_id, started_at)
            );
        """)
        ch_prov = json.dumps({"instrument": "ism", "tool": "rtl_433"})
        ch_digest = "cd" * 32
        conn.execute(
            "INSERT INTO challenges(unit_id, started_at, window_s) VALUES(4, 1000.0, 900)"
        )
        conn.execute(
            "INSERT INTO challenge_passes("
            "unit_id, started_at, passed_at, instrument, provenance, digest"
            ") VALUES(4, 1000.0, 1050.0, 'ism', ?, ?)",
            (ch_prov, ch_digest),
        )
        expected["challenges"] = [(4, 1000.0, 900)]
        expected["challenge_passes"] = [(4, 1000.0, 1050.0, "ism", ch_prov, ch_digest)]
    if with_replay:
        conn.execute("""
            CREATE TABLE replays_seen (
                instrument TEXT PRIMARY KEY,
                first_ts   REAL NOT NULL,
                count      INTEGER NOT NULL,
                recording  TEXT NOT NULL,
                digest     TEXT NOT NULL
            )
        """)
        replay_row = ("ism", 106.0, 2, "clip", "ab" * 32)
        conn.execute("INSERT INTO replays_seen VALUES(?, ?, ?, ?, ?)", replay_row)
        expected["replays_seen"] = [replay_row]
    if with_solos:
        conn.executescript("""
            CREATE TABLE solo_attempts (
                solo_id       TEXT PRIMARY KEY,
                attempt_id    TEXT NOT NULL,
                started_at    REAL NOT NULL,
                window_s      INTEGER NOT NULL,
                boot_id       TEXT NOT NULL DEFAULT '',
                attempt_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE solo_passes (
                solo_id    TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                band_slot  INTEGER NOT NULL,
                passed_at  REAL NOT NULL,
                instrument TEXT NOT NULL,
                provenance TEXT NOT NULL,
                digest     TEXT NOT NULL,
                PRIMARY KEY (solo_id, attempt_id, band_slot)
            );
            CREATE TABLE solo_calls (
                solo_id       TEXT NOT NULL,
                attempt_id    TEXT NOT NULL,
                started_at    REAL NOT NULL,
                band_slot     INTEGER NOT NULL,
                call          TEXT NOT NULL,
                why_key       TEXT NOT NULL,
                call_correct  INTEGER NOT NULL,
                why_correct   INTEGER NOT NULL,
                answered_at   REAL NOT NULL,
                item_revision TEXT NOT NULL,
                PRIMARY KEY (solo_id, attempt_id, band_slot)
            );
        """)
        solo_prov = json.dumps({"instrument": "ism", "tool": "rtl_433"})
        solo_digest = "ef" * 32
        conn.execute(
            "INSERT INTO solo_attempts(solo_id, attempt_id, started_at, window_s, boot_id, "
            "attempt_order) VALUES('u4-solo', '1', 2000.0, 900, 'boot-0', 1)"
        )
        conn.execute(
            "INSERT INTO solo_passes("
            "solo_id, attempt_id, started_at, band_slot, passed_at, instrument, provenance, digest"
            ") VALUES('u4-solo', '1', 2000.0, 0, 2050.0, 'ism', ?, ?)",
            (solo_prov, solo_digest),
        )
        conn.execute(
            "INSERT INTO solo_calls("
            "solo_id, attempt_id, started_at, band_slot, call, why_key, call_correct, "
            "why_correct, answered_at, item_revision"
            ") VALUES('u4-solo', '1', 2000.0, 0, 'legal', 'readily-accessible', 1, 1, "
            "2060.0, 'rev-1')"
        )
        expected["solo_attempts"] = [("u4-solo", "1", 2000.0, 900, "boot-0", 1)]
        expected["solo_passes"] = [
            ("u4-solo", "1", 2000.0, 0, 2050.0, "ism", solo_prov, solo_digest)
        ]
        expected["solo_calls"] = [
            ("u4-solo", "1", 2000.0, 0, "legal", "readily-accessible", 1, 1, 2060.0, "rev-1")
        ]
    conn.commit()
    conn.close()
    return expected


def _build_exam_v0(path: Path, variant: str) -> dict:
    """Create a real historical version-0 exam.db fixture and return seeded expected rows."""
    conn = sqlite3.connect(path)
    expected: dict = {}
    if variant == "empty":
        conn.commit()
        conn.close()
        return expected

    conn.executescript("""
        CREATE TABLE srs (
            question_id TEXT PRIMARY KEY,
            box         INTEGER NOT NULL DEFAULT 1,
            last_seen   REAL,
            seen        INTEGER NOT NULL DEFAULT 0,
            correct     INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.execute(
        "INSERT INTO srs(question_id, box, last_seen, seen, correct) "
        "VALUES('T1A01', 3, 200.5, 4, 3)"
    )
    expected["srs"] = [("T1A01", 3, 200.5, 4, 3)]

    if variant == "v0_b90e589":
        conn.commit()
        conn.close()
        return expected

    if variant == "v0_32ec4a0":
        conn.executescript("""
            CREATE TABLE sim (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                question_ids TEXT NOT NULL,
                answers      TEXT NOT NULL DEFAULT '{}',
                created      REAL
            );
        """)
        conn.execute(
            "INSERT INTO sim(id, question_ids, answers, created) "
            "VALUES(1, '[\"T1A01\"]', '{\"T1A01\": \"A\"}', 201.0)"
        )
        expected["sim"] = [(1, '["T1A01"]', '{"T1A01": "A"}', 201.0, "[]")]
        conn.commit()
        conn.close()
        return expected

    if variant == "v0_02c2095_upgraded":
        conn.executescript("""
            CREATE TABLE sim (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                question_ids TEXT NOT NULL,
                answers      TEXT NOT NULL DEFAULT '{}',
                created      REAL
            );
        """)
        conn.execute("ALTER TABLE sim ADD COLUMN skipped TEXT NOT NULL DEFAULT '[]'")
    else:
        assert variant == "v0_02c2095"
        conn.executescript("""
            CREATE TABLE sim (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                question_ids TEXT NOT NULL,
                answers      TEXT NOT NULL DEFAULT '{}',
                created      REAL,
                skipped      TEXT NOT NULL DEFAULT '[]'
            );
        """)
    conn.execute(
        "INSERT INTO sim(id, question_ids, answers, created, skipped) "
        "VALUES(1, '[\"T1A01\",\"T1A02\"]', '{\"T1A01\": \"A\"}', 202.0, '[\"T1A02\"]')"
    )
    expected["sim"] = [(1, '["T1A01","T1A02"]', '{"T1A01": "A"}', 202.0, '["T1A02"]')]
    conn.commit()
    conn.close()
    return expected


def _counting_write_authorizer(counter: list[str]):
    """SQLite authorizer that records any write or DDL action without blocking reads."""
    write_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_PRAGMA,
    }

    def auth(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_PRAGMA:
            # PRAGMA reads (e.g. user_version / table_info / integrity_check) have arg2 is None.
            if arg2 is not None:
                counter.append(f"PRAGMA {arg1}={arg2}")
        elif action in write_actions:
            counter.append(f"ACTION {action} {arg1}")
        return sqlite3.SQLITE_OK

    return auth


# --- §8 Test 1: v0 -> v1 migration on copies, preserves every value, second pass is no-op ------


@pytest.mark.parametrize(
    "p_variant",
    [
        "empty", "v0_72ddf9c", "v0_02c2095", "v0_e72c706", "v0_1a35a1a",
        "v0_retrieval", "v0_challenges", "v0_challenges_retrieval",
        *_REPLAY_PROGRESS_BASES, "v0_solos",
    ],
)
@pytest.mark.parametrize(
    "e_variant",
    ["empty", "v0_b90e589", "v0_32ec4a0", "v0_02c2095", "v0_02c2095_upgraded"],
)
def test_both_v0_stores_migrate_once_and_keep_every_value(tmp_path, p_variant, e_variant):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()

    src_p = src_dir / "progress.db"
    src_e = src_dir / "exam.db"
    exp_p = _build_progress_v0(src_p, p_variant)
    exp_e = _build_exam_v0(src_e, e_variant)
    orig_p_bytes = src_p.read_bytes()
    orig_e_bytes = src_e.read_bytes()

    dst_p = dst_dir / "progress.db"
    dst_e = dst_dir / "exam.db"
    scratch = tmp_path / "staging" / "sqlite"

    rp = migrate_store_copy(src_p, dst_p, store="progress", scratch_dir=scratch / "progress")
    re_ = migrate_store_copy(src_e, dst_e, store="exam", scratch_dir=scratch / "exam")

    # Source files are NEVER touched, and destination generation has only the two DBs (no scratch).
    assert src_p.read_bytes() == orig_p_bytes
    assert src_e.read_bytes() == orig_e_bytes
    assert sorted(p.name for p in dst_dir.iterdir()) == ["exam.db", "progress.db"]

    assert rp["from_version"] == 0 and rp["to_version"] == PROGRESS_SCHEMA_VERSION
    assert rp["migrated"] is True
    assert re_["from_version"] == 0 and re_["to_version"] == EXAM_SCHEMA_VERSION
    assert re_["migrated"] is True

    # Verify head=1, exact v1 schema, and every single seeded value preserved.
    conn_p = sqlite3.connect(dst_p)
    assert conn_p.execute("PRAGMA user_version").fetchone()[0] == PROGRESS_SCHEMA_VERSION
    assert schema_signature(conn_p) == reviewed_signatures("progress")["v1"]
    for table, rows in exp_p.items():
        actual = conn_p.execute(f'SELECT * FROM "{table}"').fetchall()
        assert actual == rows, (p_variant, table, actual, rows)
    conn_p.close()

    conn_e = sqlite3.connect(dst_e)
    assert conn_e.execute("PRAGMA user_version").fetchone()[0] == EXAM_SCHEMA_VERSION
    assert schema_signature(conn_e) == reviewed_signatures("exam")["v1"]
    for table, rows in exp_e.items():
        actual = conn_e.execute(f'SELECT * FROM "{table}"').fetchall()
        assert actual == rows, (e_variant, table, actual, rows)
    conn_e.close()

    # Second open in managed mode must perform ZERO writes (verified via SQLite write authorizer).
    writes_p: list[str] = []
    prog = Progress(dst_p, managed=True)
    prog._conn.set_authorizer(_counting_write_authorizer(writes_p))
    assert prog.get(1) is not None or p_variant == "empty"
    if p_variant in (
        "v0_e72c706", "v0_1a35a1a", "v0_retrieval", "v0_challenges", "v0_challenges_retrieval",
        *_REPLAY_PROGRESS_BASES, "v0_solos",
    ):
        assert len(prog.stamps()) == 1
        assert prog.stamps()[0]["count"] == 3
    prog.close()
    assert writes_p == [], f"managed Progress open/read wrote: {writes_p}"

    writes_e: list[str] = []
    exam = ExamStore(dst_e, managed=True)
    exam._conn.set_authorizer(_counting_write_authorizer(writes_e))
    assert exam.state("T1A01") is not None or e_variant == "empty"
    exam.close()
    assert writes_e == [], f"managed ExamStore open/read wrote: {writes_e}"

    # Re-migrating an already-v1 copy reports migrated=False and keeps exact bytes/rows.
    dst2_dir = tmp_path / "dst2"
    dst2_dir.mkdir()
    dst_p2 = dst2_dir / "progress.db"
    dst_e2 = dst2_dir / "exam.db"
    rp2 = migrate_store_copy(dst_p, dst_p2, store="progress", scratch_dir=scratch / "progress2")
    re2 = migrate_store_copy(dst_e, dst_e2, store="exam", scratch_dir=scratch / "exam2")
    assert rp2["migrated"] is False and rp2["from_version"] == 1 and rp2["to_version"] == 1
    assert re2["migrated"] is False and re2["from_version"] == 1 and re2["to_version"] == 1


def test_missing_legacy_store_bootstraps_v1_only_when_explicitly_allowed(tmp_path):
    missing = tmp_path / "absent.db"
    gen = tmp_path / "gen"
    gen.mkdir()
    dst = gen / "bootstrapped.db"
    with pytest.raises(SchemaError, match="missing"):
        migrate_store_copy(
            missing, dst, store="progress", allow_missing_source=False,
            scratch_dir=tmp_path / "scratch1",
        )
    assert not dst.exists()

    receipt = migrate_store_copy(
        missing, dst, store="progress", allow_missing_source=True,
        scratch_dir=tmp_path / "scratch2",
    )
    assert receipt["from_version"] == 0 and receipt["to_version"] == 1
    assert not missing.exists()
    prog = Progress(dst, managed=True)
    assert prog.stamps() == []
    prog.close()


# --- §8 Test 2: failed migration rolls back schema, rows, and user_version --------------------


@pytest.mark.parametrize("store", ["progress", "exam"])
@pytest.mark.parametrize("fail_point", ["after_step", "after_version"])
def test_failed_migration_rolls_back_schema_rows_and_version(tmp_path, store, fail_point):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / f"src_{store}.db"
    gen = tmp_path / "gen"
    gen.mkdir()
    dst = gen / f"dst_{store}.db"
    if store == "progress":
        expected = _build_progress_v0(src, "v0_72ddf9c")
    else:
        expected = _build_exam_v0(src, "v0_32ec4a0")

    src_bytes = src.read_bytes()
    src_conn = sqlite3.connect(src)
    src_sig = schema_signature(src_conn)
    src_conn.close()

    def boom(_conn):
        raise RuntimeError(f"injected failure at {fail_point}")

    kwargs = (
        {"_after_step_hook": boom} if fail_point == "after_step"
        else {"_after_version_hook": boom}
    )
    with pytest.raises(RuntimeError, match=fail_point):
        migrate_store_copy(src, dst, store=store, scratch_dir=tmp_path / "scratch", **kwargs)

    # Source DB is completely untouched.
    assert src.read_bytes() == src_bytes

    # Staged DB rolled back cleanly: exact pre-migration schema, rows, and user_version == 0.
    conn = sqlite3.connect(dst)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert schema_signature(conn) == src_sig
    for table, rows in expected.items():
        actual = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        # Strip migrated-only default column if checking pre-migration table
        if store == "progress" and table == "responses":
            assert actual == [r[:4] for r in rows]
        elif store == "exam" and table == "sim":
            assert actual == [r[:4] for r in rows]
        else:
            assert actual == rows
    conn.close()


# --- §8 Test 3: unknown schema or unsupported head refused before any write --------------------


@pytest.mark.parametrize("store", ["progress", "exam"])
@pytest.mark.parametrize("bad_kind", ["extra_table", "unsupported_head", "v1_corrupt_schema"])
def test_unknown_schema_or_head_is_refused_before_any_write(tmp_path, store, bad_kind):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / f"bad_{store}_{bad_kind}.db"
    gen = tmp_path / "gen"
    gen.mkdir()
    dst = gen / f"out_{store}_{bad_kind}.db"
    if store == "progress":
        _build_progress_v0(src, "v0_1a35a1a")
    else:
        _build_exam_v0(src, "v0_02c2095")

    conn = sqlite3.connect(src)
    if bad_kind == "extra_table":
        conn.execute("CREATE TABLE rogue_table (id INTEGER PRIMARY KEY)")
    elif bad_kind == "unsupported_head":
        conn.execute("PRAGMA user_version = 99")
    elif bad_kind == "v1_corrupt_schema":
        conn.execute("PRAGMA user_version = 1")
        conn.execute("CREATE TABLE unexpected_v1_table (x TEXT)")
    conn.commit()
    conn.close()

    orig_bytes = src.read_bytes()
    with pytest.raises(SchemaError) as exc_info:
        migrate_store_copy(src, dst, store=store, scratch_dir=tmp_path / "scratch")

    msg = str(exc_info.value).lower()
    if bad_kind == "unsupported_head":
        assert "99" in msg or "head" in msg or "version" in msg
    else:
        assert "unrecognized" in msg or "schema" in msg or "unexpected" in msg
    assert src.read_bytes() == orig_bytes

    # Managed open must also refuse without touching a byte.
    cls = Progress if store == "progress" else ExamStore
    with pytest.raises(SchemaError):
        cls(src, managed=True)
    assert src.read_bytes() == orig_bytes


# --- §8 Test 4: managed open never creates or migrates stores (writable and read-only trial) ---


@pytest.mark.parametrize("store, cls", [("progress", Progress), ("exam", ExamStore)])
@pytest.mark.parametrize("read_only", [False, True], ids=["writable", "trial-ro"])
def test_managed_open_does_not_create_or_migrate_stores(tmp_path, store, cls, read_only):
    # Missing file -> refused, file and parent directory NOT created.
    missing = tmp_path / "no_such_dir" / f"{store}.db"
    with pytest.raises(SchemaError, match="missing"):
        cls(missing, managed=True, read_only=read_only)
    assert not missing.exists()
    assert not missing.parent.exists()

    # Stale v0 DB -> refused with specific version/schema reason, NO DDL executed, bytes unchanged.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    stale = src_dir / f"stale_{store}.db"
    if store == "progress":
        _build_progress_v0(stale, "v0_72ddf9c")
    else:
        _build_exam_v0(stale, "v0_32ec4a0")
    before = stale.read_bytes()

    with pytest.raises(SchemaError, match="version|schema|migrate"):
        cls(stale, managed=True, read_only=read_only)
    assert stale.read_bytes() == before

    # Migrated v1 DB opens cleanly in both managed writable and read-only trial modes;
    # read-only trial mode rejects any write attempt.
    gen = tmp_path / "gen"
    gen.mkdir()
    migrated = gen / f"v1_{store}.db"
    migrate_store_copy(stale, migrated, store=store, scratch_dir=tmp_path / "scratch")
    inst = cls(migrated, managed=True, read_only=read_only)
    try:
        if read_only:
            with pytest.raises(sqlite3.OperationalError):
                if store == "progress":
                    inst.mark_opened(1)
                else:
                    inst.record("T1A01", True)
    finally:
        inst.close()


# --- §8 Test 5: acknowledged learning and assets are synced before success ---------------------


def test_acknowledged_learning_and_assets_are_synced_before_success(tmp_path, monkeypatch):
    # 1) Event log file + directory sync order, including creation and rotation.
    calls: list[tuple[str, str]] = []
    real_fsync_handle = storage_mod.fsync_handle
    real_fsync_dir = storage_mod.fsync_dir

    def spy_handle(handle):
        calls.append(("file", Path(handle.name).name))
        real_fsync_handle(handle)

    def spy_dir(path):
        calls.append(("dir", Path(path).name))
        real_fsync_dir(path)

    monkeypatch.setattr(events_mod, "fsync_handle", spy_handle)
    monkeypatch.setattr(events_mod, "fsync_dir", spy_dir)

    log_dir = tmp_path / "logbox"
    log_path = log_dir / "events.jsonl"
    # First write creates the file -> must sync file AND parent dir.
    events_mod.log_event(log_path, "complete", {"lesson": 1}, sync=True)
    assert calls == [("file", "events.jsonl"), ("dir", "logbox")]

    # Second write without rotation -> syncs file (dir unchanged).
    calls.clear()
    events_mod.log_event(log_path, "complete", {"lesson": 2}, sync=True, max_bytes=10_000)
    assert calls == [("file", "events.jsonl")]

    # Third write triggers rotation -> syncs dir after rename, then new file, then dir for new file.
    calls.clear()
    events_mod.log_event(log_path, "complete", {"lesson": 3}, sync=True, max_bytes=1, keep=2)
    assert calls[0] == ("dir", "logbox")
    assert ("file", "events.jsonl") in calls
    assert calls[-1] == ("dir", "logbox")

    # 2) Managed /api/complete route: syncs event log before returning 200; injected sync failure
    # prevents success acknowledgement WITHOUT erasing the already-committed DB row!
    gen_dir = tmp_path / "gen"
    gen_dir.mkdir()
    migrate_store_copy(tmp_path / "m_p.db", gen_dir / "progress.db",
                       store="progress", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_p")
    migrate_store_copy(tmp_path / "m_e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_e")

    app = create_app(data_dir=gen_dir, probe=_absent, managed=True)
    with TestClient(app) as client:
        calls.clear()
        pred = client.post("/api/predict?lesson_id=1", data={"prediction": "a"},
                           follow_redirects=False)
        assert pred.status_code == 303
        assert ("file", "events.jsonl") in calls

        # Inject a sync failure on /api/complete: route must NOT return 200, and the already-
        # committed progress row stays intact (never erased to imitate a failed request).
        def fail_fsync(_handle):
            raise OSError("injected disk sync failure")

        monkeypatch.setattr(events_mod, "fsync_handle", fail_fsync)
        failed = client.post("/api/complete?lesson_id=1")
        assert failed.status_code >= 500
        assert app.state.progress.get(1)["completed"] == 1

    # 3) Completed media assets:
    # SSTV (F7): fsyncs staged PNG + staged sidecar BEFORE os.link publishes them into the
    # gallery, then fsyncs the gallery directory. If staged fsync fails, neither file is linked.
    media_calls: list[tuple[str, str]] = []
    real_fsync_file = storage_mod.fsync_file
    real_link = os.link

    def spy_media_file(path):
        media_calls.append(("file", Path(path).name))
        real_fsync_file(path)

    def spy_media_dir(path):
        media_calls.append(("dir", Path(path).name))
        real_fsync_dir(path)

    def spy_link(src, dst):
        media_calls.append(("link", Path(dst).name))
        real_link(src, dst)

    monkeypatch.setattr(storage_mod, "fsync_file", spy_media_file)
    monkeypatch.setattr(storage_mod, "fsync_dir", spy_media_dir)
    monkeypatch.setattr(sstv_mod, "fsync_file", spy_media_file)
    monkeypatch.setattr(sstv_mod, "fsync_dir", spy_media_dir)
    monkeypatch.setattr(os, "link", spy_link)

    sstv_dir = tmp_path / "sstv"
    sstv_dir.mkdir()
    sstv = SstvDecoder(output_dir=sstv_dir, data_dir=tmp_path, sync_assets=True)
    staged = sstv_dir / ".staging" / "cap.png"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    published = sstv_dir / "20261128_000001_martin1.png"
    cap = _Capture(staged_path=staged, published_path=published, freq_hz=145_800_000,
                   decoded_at=100.0, mode="Martin 1")
    assert sstv._publish_capture(cap) is True
    assert media_calls == [
        ("file", staged.name),
        ("file", staged.with_suffix(".json").name),
        ("link", published.name),
        ("link", published.with_suffix(".json").name),
        ("dir", "sstv"),
    ]

    # Inject media sync failure -> _publish_capture returns False AND never links into gallery.
    def fail_media_file(_path):
        raise OSError("media fsync failed")

    monkeypatch.setattr(storage_mod, "fsync_file", fail_media_file)
    monkeypatch.setattr(sstv_mod, "fsync_file", fail_media_file)
    staged2 = sstv_dir / ".staging" / "cap2.png"
    staged2.write_bytes(b"\x89PNG\r\n\x1a\nfake2")
    published2 = sstv_dir / "20261128_000002_martin1.png"
    cap2 = _Capture(staged_path=staged2, published_path=published2, freq_hz=145_800_000,
                    decoded_at=101.0, mode="Martin 1")
    assert sstv._publish_capture(cap2) is False
    assert sstv.error and "media fsync failed" in sstv.error
    assert not published2.exists()
    assert not published2.with_suffix(".json").exists()
    assert all(Path(img["path"]).name != published2.name for img in sstv.images())

    # APT media publication sync fence: restores working spy, verifies sync before emit, then
    # injects failure and verifies emit is blocked.
    monkeypatch.setattr(storage_mod, "fsync_file", spy_media_file)
    monkeypatch.setattr(os, "link", real_link)
    apt_dir = tmp_path / "apt"
    apt_dir.mkdir()
    apt = AptDecoder(output_dir=apt_dir, data_dir=tmp_path, sync_assets=True)
    emitted = []
    apt.on_reception = lambda item, **kw: emitted.append(item)
    png = apt_dir / "pass1.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\napt")
    media_calls.clear()
    assert apt._sync_published_image(png) is True
    assert media_calls == [("file", "pass1.png"), ("dir", "apt")]

    monkeypatch.setattr(storage_mod, "fsync_file", fail_media_file)
    assert apt._sync_published_image(png) is False
    assert apt.error and "media fsync failed" in apt.error


# --- §8 Test 6: managed live connections use synchronous=EXTRA (3) and journal_mode=delete -----


def test_managed_live_connections_use_extra_delete(tmp_path):
    gen_dir = tmp_path / "managed_gen"
    gen_dir.mkdir()
    migrate_store_copy(tmp_path / "none_p.db", gen_dir / "progress.db",
                       store="progress", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_p")
    migrate_store_copy(tmp_path / "none_e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_e")

    app = create_app(data_dir=gen_dir, probe=_absent, managed=True)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        for name, conn in (("progress", app.state.progress._conn),
                           ("exam", app.state.exam_store._conn)):
            sync_val = conn.execute("PRAGMA synchronous").fetchone()[0]
            jmode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
            assert sync_val == 3, f"{name}: expected synchronous=3 (EXTRA), got {sync_val}"
            assert jmode == "delete", f"{name}: expected journal_mode=delete, got {jmode}"

        # Overriding either PRAGMA on either live connection must fail verification.
        for name, conn, expected_ver in (
            ("progress", app.state.progress._conn, PROGRESS_SCHEMA_VERSION),
            ("exam", app.state.exam_store._conn, EXAM_SCHEMA_VERSION),
        ):
            expected_sig = reviewed_signatures(name)["v1"]
            verify_managed_connection(conn, expected_version=expected_ver,
                                      expected_signature=expected_sig)
            conn.execute("PRAGMA synchronous = FULL")
            with pytest.raises(SchemaError, match="synchronous"):
                verify_managed_connection(conn, expected_version=expected_ver,
                                          expected_signature=expected_sig)
            conn.execute("PRAGMA synchronous = EXTRA")

            conn.execute("PRAGMA journal_mode = WAL")
            with pytest.raises(SchemaError, match="journal_mode"):
                verify_managed_connection(conn, expected_version=expected_ver,
                                          expected_signature=expected_sig)
            conn.execute("PRAGMA journal_mode = DELETE")


# --- §8 Test 7: live legacy CI guard (current constructors must match reviewed v0 set) ---------


def test_current_legacy_constructor_signatures_are_in_reviewed_set(tmp_path):
    # Construct fresh legacy stores with today's real constructors and require exact matches in
    # the reviewed version-zero signature set.
    p_path = tmp_path / "legacy_progress.db"
    e_path = tmp_path / "legacy_exam.db"
    prog = Progress(p_path)
    exam = ExamStore(e_path)
    try:
        p_sig = schema_signature(prog._conn)
        e_sig = schema_signature(exam._conn)
        assert prog._conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert exam._conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        prog.close()
        exam.close()

    assert match_v0_signature("progress", p_sig) is not None, (
        "Current legacy Progress constructor schema is not in the reviewed v0 fixture set! "
        "Review and add its migration before shipping."
    )
    assert match_v0_signature("exam", e_sig) is not None, (
        "Current legacy ExamStore constructor schema is not in the reviewed v0 fixture set! "
        "Review and add its migration before shipping."
    )

    # Prove the CI guard catches dropping any stamp/sort table (leaving an unreviewed partial
    # schema) or adding an unreviewed table.
    for drop_table in ("stamps", "stamp_events", "sort_answers"):
        mutated = copy.deepcopy(p_sig)
        del mutated["tables"][drop_table]
        assert match_v0_signature("progress", mutated) is None

    unreviewed = copy.deepcopy(p_sig)
    unreviewed["tables"]["unreviewed_feature"] = {
        "columns": [[0, "id", "INTEGER", 0, None, 1]],
        "indexes": [],
        "without_rowid": False,
        "checks": [],
        "foreign_keys": [],
    }
    assert match_v0_signature("progress", unreviewed) is None

    # Exercise migrated pre-PR-134 and PR-134/current DBs with seeded first evidence & watermarks.
    pre_src = tmp_path / "pre_src"
    pre_src.mkdir()
    pre_134 = pre_src / "pre134.db"
    _build_progress_v0(pre_134, "v0_02c2095")
    pre_gen = tmp_path / "pre_gen"
    pre_gen.mkdir()
    pre_out = pre_gen / "progress.db"
    migrate_store_copy(pre_134, pre_out, store="progress", scratch_dir=tmp_path / "scratch_pre")
    p_pre = Progress(pre_out, managed=True)
    assert p_pre.stamps() == []
    assert p_pre.get(1)["completed"] == 1
    p_pre.close()

    pr_src = tmp_path / "pr_src"
    pr_src.mkdir()
    pr_134 = pr_src / "pr134.db"
    _build_progress_v0(pr_134, "v0_e72c706")
    pr_gen = tmp_path / "pr_gen"
    pr_gen.mkdir()
    pr_out = pr_gen / "progress.db"
    migrate_store_copy(pr_134, pr_out, store="progress", scratch_dir=tmp_path / "scratch_pr")
    p_134 = Progress(pr_out, managed=True)
    stamps = p_134.stamps()
    assert len(stamps) == 1 and stamps[0]["instrument"] == "ism" and stamps[0]["count"] == 3
    p_134.close()


# --- WAL recovery on private copy + R2-4 root check + managed boot smoke ----------------------


def test_wal_source_is_recovered_on_private_copy_without_mutating_source(tmp_path):
    live = tmp_path / "live_progress.db"
    _build_progress_v0(live, "v0_1a35a1a")
    conn = sqlite3.connect(live)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute(
        "INSERT INTO progress(lesson_id, opened, completed, updated) VALUES(9, 1, 1, 999.0)"
    )
    conn.commit()
    # Snapshot a quiesced uncheckpointed WAL pair (as after a power cut) before closing live conn.
    src_dir = tmp_path / "wal_src"
    src_dir.mkdir()
    src = src_dir / "wal_progress.db"
    shutil.copy2(live, src)
    shutil.copy2(Path(f"{live}-wal"), Path(f"{src}-wal"))
    if Path(f"{live}-shm").exists():
        shutil.copy2(Path(f"{live}-shm"), Path(f"{src}-shm"))
    conn.close()

    wal_path = Path(f"{src}-wal")
    assert wal_path.exists() and wal_path.stat().st_size > 0
    src_before = src.read_bytes()
    wal_before = wal_path.read_bytes()

    gen = tmp_path / "gen"
    gen.mkdir()
    dst = gen / "recovered_v1.db"
    receipt = migrate_store_copy(src, dst, store="progress", scratch_dir=tmp_path / "scratch_wal")

    assert receipt["to_version"] == 1
    assert src.read_bytes() == src_before
    assert wal_path.read_bytes() == wal_before
    # Migrated copy recovered lesson 9 from the WAL and is now standalone in DELETE mode.
    prog = Progress(dst, managed=True)
    assert prog.get(9)["completed"] == 1
    assert prog._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    prog.close()


def test_r2_4_updater_root_never_uses_local_share_sigint101(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    forbidden = fake_home / FORBIDDEN_ROOT_REL
    assert FORBIDDEN_ROOT_REL == Path(".local/share/sigint101")
    for bad in (
        forbidden,
        forbidden / "nested",
        fake_home,
        fake_home / ".local",
        fake_home / ".local" / "share",
    ):
        with pytest.raises(ValueError, match="local/share/sigint101"):
            validate_updater_root(bad, home=fake_home)
    with pytest.raises(ValueError, match="absolute"):
        validate_updater_root(Path("relative/root"), home=fake_home)

    allowed = fake_home / DEFAULT_UPDATER_ROOT_REL
    assert validate_updater_root(allowed, home=fake_home) == allowed.resolve()


def test_managed_boot_smoke_with_site_env_change(tmp_path, monkeypatch):
    # Smoke check for managed boot across env changes; real R2-1 parity check lands in PR 2.
    gen_dir = tmp_path / "gen"
    gen_dir.mkdir()
    migrate_store_copy(tmp_path / "p.db", gen_dir / "progress.db",
                       store="progress", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_p")
    migrate_store_copy(tmp_path / "e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_e")

    for serial in ("00000001", "00000002"):
        monkeypatch.setenv("SIGINT101_RTLSDR_SERIAL", serial)
        app = create_app(data_dir=gen_dir, probe=_absent, managed=True)
        with TestClient(app) as client:
            assert client.get("/").status_code == 200


# --- F1: signature catches nested-paren CHECK, COLLATE, AUTOINCREMENT, ON CONFLICT, generated --


@pytest.mark.parametrize(
    "mutation_name, old_sql, new_sql",
    [
        (
            "nested_check",
            "correct   INTEGER NOT NULL,",
            "correct   INTEGER NOT NULL CHECK (correct IN (0, 1)),",
        ),
        (
            "collate_nocase",
            "name    TEXT PRIMARY KEY,",
            "name    TEXT PRIMARY KEY COLLATE NOCASE,",
        ),
        (
            "autoincrement",
            "lesson_id INTEGER PRIMARY KEY,",
            "lesson_id INTEGER PRIMARY KEY AUTOINCREMENT,",
        ),
        (
            "on_conflict",
            "stage     TEXT NOT NULL,",
            "stage     TEXT NOT NULL ON CONFLICT REPLACE,",
        ),
        (
            "generated_column",
            "updated   REAL,\n    PRIMARY KEY (lesson_id, card_key)",
            "updated   REAL,\n    label TEXT GENERATED ALWAYS AS (card_key || ':' || choice) "
            "VIRTUAL,\n    PRIMARY KEY (lesson_id, card_key)",
        ),
    ],
)
def test_schema_signature_catches_five_constraint_mutations(
    tmp_path, mutation_name, old_sql, new_sql
):
    from sigint101 import progress as progress_mod

    assert old_sql in progress_mod.SCHEMA
    mutated_schema = progress_mod.SCHEMA.replace(old_sql, new_sql, 1)
    db_path = tmp_path / f"mut_{mutation_name}.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(mutated_schema)
    progress_mod._add_correct_column(conn)
    conn.commit()
    sig = schema_signature(conn)

    # Guard must reject the mutated v0 schema (not in reviewed set).
    assert match_v0_signature("progress", sig) is None, mutation_name
    assert sig != reviewed_signatures("progress")["v1"], mutation_name

    # Even if stamped user_version = 1, managed open must refuse the mutated store.
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaError, match="signature"):
        Progress(db_path, managed=True)


# --- F3: read-only source with a hot journal migrates cleanly without touching source ----------


def test_read_only_hot_journal_source_migrates_without_touching_source(tmp_path):
    src_dir = tmp_path / "ro_src"
    src_dir.mkdir()
    src = src_dir / "progress.db"
    _build_progress_v0(src, "v0_1a35a1a")

    # Produce a real hot journal: a child opens a transaction with cache_size=1 so dirty pages
    # spill into progress.db + progress.db-journal, then dies with SIGKILL before committing.
    killer = (
        "import os, signal, sqlite3, sys\n"
        "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "conn.execute('PRAGMA journal_mode = DELETE')\n"
        "conn.execute('PRAGMA cache_size = 1')\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "for i in range(100, 180):\n"
        "    conn.execute('INSERT INTO progress(lesson_id, opened, completed, updated) "
        "VALUES(?, 1, 1, 500.0)', (i,))\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    proc = subprocess.run([sys.executable, "-c", killer, str(src)], check=False)
    assert proc.returncode != 0
    journal = Path(f"{src}-journal")
    assert journal.exists() and journal.stat().st_size > 0

    src_before = src.read_bytes()
    journal_before = journal.read_bytes()
    src.chmod(0o444)
    journal.chmod(0o444)
    src_dir.chmod(0o555)
    try:
        gen = tmp_path / "gen"
        gen.mkdir()
        dst = gen / "progress.db"
        receipt = migrate_store_copy(
            src, dst, store="progress", scratch_dir=tmp_path / "staging" / "ro_hot"
        )
        assert receipt["to_version"] == 1
        assert src.read_bytes() == src_before
        assert journal.read_bytes() == journal_before
        assert (src.stat().st_mode & 0o777) == 0o444
        assert (journal.stat().st_mode & 0o777) == 0o444

        prog = Progress(dst, managed=True)
        try:
            # Committed pre-crash row kept; uncommitted hot-journal rows rolled back.
            assert prog.get(1)["completed"] == 1
            assert prog.get(100) is None
        finally:
            prog.close()
    finally:
        src_dir.chmod(0o700)
        src.chmod(0o600)
        journal.chmod(0o600)


# --- F4: recovery scratch lives outside destination generation, never recursively removed ------


def test_recovery_scratch_lives_outside_generation_and_is_not_recursively_removed(
    tmp_path, monkeypatch
):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "src.db"
    _build_progress_v0(src, "v0_1a35a1a")
    gen = tmp_path / "gen"
    gen.mkdir()
    dst = gen / "progress.db"

    # Refuse scratch equal to or nested under destination generation.
    for bad_scratch in (gen, gen / "scratch", gen / "nested" / "sqlite"):
        with pytest.raises(ValueError, match="generation|destination"):
            migrate_store_copy(src, dst, store="progress", scratch_dir=bad_scratch)
    assert list(gen.iterdir()) == []

    # Refuse non-exclusive (already-existing) scratch directory.
    existing_scratch = tmp_path / "already_exists"
    existing_scratch.mkdir()
    with pytest.raises(FileExistsError):
        migrate_store_copy(src, dst, store="progress", scratch_dir=existing_scratch)
    assert list(gen.iterdir()) == []

    # Forbid rmtree completely during migration; scratch must stay outside gen and be preserved.
    def forbid_rmtree(*_args, **_kwargs):
        raise AssertionError("migrate_store_copy must never call shutil.rmtree")

    monkeypatch.setattr(shutil, "rmtree", forbid_rmtree)
    scratch = tmp_path / "staging" / "op1" / "sqlite" / "progress"
    receipt = migrate_store_copy(src, dst, store="progress", scratch_dir=scratch)
    assert receipt["to_version"] == 1
    assert [p.name for p in gen.iterdir()] == ["progress.db"]
    assert (scratch / src.name).is_file()


# --- F5: unfenced create or rotation is directory-synced on the next acknowledged write --------


def test_events_dir_synced_after_unfenced_create_or_rotation(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    real_fsync_handle = storage_mod.fsync_handle
    real_fsync_dir = storage_mod.fsync_dir

    def spy_handle(handle):
        calls.append(("file", Path(handle.name).name))
        real_fsync_handle(handle)

    def spy_dir(path):
        calls.append(("dir", Path(path).name))
        real_fsync_dir(path)

    monkeypatch.setattr(events_mod, "fsync_handle", spy_handle)
    monkeypatch.setattr(events_mod, "fsync_dir", spy_dir)

    # 1) Direct log_event: unfenced create -> next fenced write MUST fsync dir.
    log_dir = tmp_path / "f5_direct"
    log_path = log_dir / "events.jsonl"
    events_mod.log_event(log_path, "completion_refused", {"lesson": 1}, sync=False)
    assert calls == []

    events_mod.log_event(log_path, "prediction", {"lesson": 1}, sync=True)
    assert calls == [("file", "events.jsonl"), ("dir", "f5_direct")]

    # Steady-state fenced write on the already-synced inode syncs only the file.
    calls.clear()
    events_mod.log_event(log_path, "check", {"lesson": 1}, sync=True, max_bytes=10_000)
    assert calls == [("file", "events.jsonl")]

    # 2) Direct log_event: unfenced rotation -> next fenced write MUST fsync dir.
    calls.clear()
    events_mod.log_event(log_path, "tune", {"center_hz": 100_000_000},
                         sync=False, max_bytes=1, keep=2)
    assert calls == []
    assert (log_dir / "events.jsonl.1").is_file()

    events_mod.log_event(log_path, "completed", {"lesson": 1}, sync=True, max_bytes=10_000)
    assert calls == [("file", "events.jsonl"), ("dir", "f5_direct")]

    # 3) Failed dir fsync after unfenced create must NOT mark the inode synced.
    retry_dir = tmp_path / "f5_retry"
    retry_log = retry_dir / "events.jsonl"
    events_mod.log_event(retry_log, "tune", {"x": 1}, sync=False)
    calls.clear()

    def fail_dir_once(_path):
        raise OSError("transient dir sync failure")

    monkeypatch.setattr(events_mod, "fsync_dir", fail_dir_once)
    with pytest.raises(OSError, match="transient dir sync failure"):
        events_mod.log_event(retry_log, "prediction", {"lesson": 1}, sync=True)

    monkeypatch.setattr(events_mod, "fsync_dir", spy_dir)
    calls.clear()
    events_mod.log_event(retry_log, "prediction", {"lesson": 1}, sync=True)
    assert calls == [("file", "events.jsonl"), ("dir", "f5_retry")]

    # 4) End-to-end managed app: refused completion (unfenced create) followed by /api/predict.
    gen_dir = tmp_path / "f5_gen"
    gen_dir.mkdir()
    migrate_store_copy(tmp_path / "p.db", gen_dir / "progress.db",
                       store="progress", allow_missing_source=True,
                       scratch_dir=tmp_path / "f5_scratch_p")
    migrate_store_copy(tmp_path / "e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True,
                       scratch_dir=tmp_path / "f5_scratch_e")
    app = create_app(data_dir=gen_dir, probe=_absent, managed=True)
    with TestClient(app) as client:
        calls.clear()
        refused = client.post("/api/complete?lesson_id=1")
        assert refused.status_code == 400
        assert calls == []
        assert (gen_dir / "events.jsonl").is_file()

        pred = client.post("/api/predict?lesson_id=1", data={"prediction": "a"},
                           follow_redirects=False)
        assert pred.status_code == 303
        assert ("file", "events.jsonl") in calls
        assert ("dir", "f5_gen") in calls


# --- F6: main-side / legacy baseline comparisons required by plan §8 ---------------------------


def test_legacy_baseline_differs_from_managed_contract(tmp_path, monkeypatch):
    # 1) Legacy constructors evolve stale v0 stores IN PLACE and keep user_version == 0.
    stale_p = tmp_path / "legacy_stale_progress.db"
    stale_e = tmp_path / "legacy_stale_exam.db"
    _build_progress_v0(stale_p, "v0_72ddf9c")
    _build_exam_v0(stale_e, "v0_32ec4a0")
    before_p = stale_p.read_bytes()
    before_e = stale_e.read_bytes()

    prog = Progress(stale_p)
    exam = ExamStore(stale_e)
    try:
        assert prog._conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert exam._conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "sort_answers" in schema_signature(prog._conn)["tables"]
        assert "skipped" in {
            c[1] for c in schema_signature(exam._conn)["tables"]["sim"]["columns"]
        }
    finally:
        prog.close()
        exam.close()
    assert stale_p.read_bytes() != before_p
    assert stale_e.read_bytes() != before_e

    # 2) Legacy constructors open a store with an extra table and head 99 without refusing.
    rogue_p = tmp_path / "rogue_progress.db"
    rogue_e = tmp_path / "rogue_exam.db"
    _build_progress_v0(rogue_p, "v0_1a35a1a")
    _build_exam_v0(rogue_e, "v0_02c2095")
    for path in (rogue_p, rogue_e):
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE rogue_table (id INTEGER PRIMARY KEY)")
        c.execute("PRAGMA user_version = 99")
        c.commit()
        c.close()
    p_rogue = Progress(rogue_p)
    e_rogue = ExamStore(rogue_e)
    try:
        assert p_rogue._conn.execute("PRAGMA user_version").fetchone()[0] == 99
        assert e_rogue._conn.execute("PRAGMA user_version").fetchone()[0] == 99
    finally:
        p_rogue.close()
        e_rogue.close()

    # 3) Legacy constructors create missing files and parent directories on open.
    missing_p = tmp_path / "legacy_auto_dir" / "progress.db"
    missing_e = tmp_path / "legacy_auto_dir" / "exam.db"
    assert not missing_p.parent.exists()
    p_new = Progress(missing_p)
    e_new = ExamStore(missing_e)
    try:
        assert missing_p.is_file() and missing_e.is_file()
        assert p_new._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert e_new._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        p_new.close()
        e_new.close()

    # 4) Legacy app runs synchronous=2 and completes lessons with zero Python fsync calls.
    fsync_calls: list[str] = []
    monkeypatch.setattr(events_mod, "fsync_handle", lambda _h: fsync_calls.append("file"))
    monkeypatch.setattr(events_mod, "fsync_dir", lambda _p: fsync_calls.append("dir"))
    legacy_dir = tmp_path / "legacy_app"
    app = create_app(data_dir=legacy_dir, probe=_absent)
    with TestClient(app) as client:
        assert app.state.progress._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert app.state.exam_store._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        pred = client.post("/api/predict?lesson_id=1", data={"prediction": "a"},
                           follow_redirects=False)
        assert pred.status_code == 303
        comp = client.post("/api/complete?lesson_id=1")
        assert comp.status_code == 200
        assert fsync_calls == []


# --- F9, F11, F14 one-liner NIT regressions ----------------------------------------------------


def test_legacy_event_log_oserror_not_rewritten_as_sync_error(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "legacy_enospc"
    app = create_app(data_dir=legacy_dir, probe=_absent)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/predict?lesson_id=1", data={"prediction": "a"}, follow_redirects=False
        ).status_code == 303

        def boom(*_args, **_kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr("sigint101.app.log_event", boom)
        res = client.post("/api/complete?lesson_id=1")
        assert res.status_code == 500
        assert "event log sync failed" not in res.text


def test_managed_open_refuses_wal_without_touching_disk_bytes(tmp_path):
    gen = tmp_path / "wal_managed"
    gen.mkdir()
    db_path = gen / "progress.db"
    migrate_store_copy(tmp_path / "none.db", db_path, store="progress",
                       allow_missing_source=True, scratch_dir=tmp_path / "scratch_w")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("INSERT INTO progress(lesson_id, opened, completed, updated) VALUES(1, 1, 0, 1.0)")
    conn.commit()
    # Copy active WAL triple before closing so -wal and -shm are present on disk.
    snap_dir = tmp_path / "wal_snap"
    snap_dir.mkdir()
    snap = snap_dir / "progress.db"
    shutil.copy2(db_path, snap)
    shutil.copy2(Path(f"{db_path}-wal"), Path(f"{snap}-wal"))
    shutil.copy2(Path(f"{db_path}-shm"), Path(f"{snap}-shm"))
    conn.close()

    before_db = snap.read_bytes()
    before_wal = Path(f"{snap}-wal").read_bytes()
    before_shm = Path(f"{snap}-shm").read_bytes()

    for read_only in (False, True):
        with pytest.raises(SchemaError, match="journal_mode=delete"):
            open_managed_sqlite(
                snap,
                expected_version=PROGRESS_SCHEMA_VERSION,
                expected_signature=reviewed_signatures("progress")["v1"],
                read_only=read_only,
            )
        assert snap.read_bytes() == before_db
        assert Path(f"{snap}-wal").read_bytes() == before_wal
        assert Path(f"{snap}-shm").read_bytes() == before_shm


def test_migration_row_preservation_distinguishes_int_from_float(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val)")
    conn.execute("INSERT INTO t(id, val) VALUES(1, 1)")
    conn.commit()
    sig = schema_signature(conn)
    before = _snapshot_rows(conn, sig["tables"])
    conn.execute("UPDATE t SET val = 1.0 WHERE id = 1")
    with pytest.raises(SchemaError, match="altered pre-existing rows"):
        _verify_preserved_rows(conn, before)
    conn.close()


# --- PR 1 Round 2 build-review leftovers (coverage gaps & R2-a/R2-b/F14 residuals) -------------


def test_migration_refusals_cleanup_and_scratch_boundary_checks(tmp_path, monkeypatch):
    # 1) Destination already exists is refused without overwriting existing destination bytes.
    src_dir = tmp_path / "src_exists"
    src_dir.mkdir()
    src = src_dir / "progress.db"
    _build_progress_v0(src, "v0_1a35a1a")
    dst_dir = tmp_path / "dst_exists"
    dst_dir.mkdir()
    dst = dst_dir / "progress.db"
    dst.write_bytes(b"existing-sentinel")
    with pytest.raises(FileExistsError, match="already exists"):
        migrate_store_copy(src, dst, store="progress", scratch_dir=tmp_path / "scratch_exists")
    assert dst.read_bytes() == b"existing-sentinel"

    # 2) Orphan sidecar without a main database is refused even when allow_missing_source=True.
    orphan_dir = tmp_path / "orphan_src"
    orphan_dir.mkdir()
    orphan_db = orphan_dir / "progress.db"
    Path(f"{orphan_db}-wal").write_bytes(b"stray-wal")
    with pytest.raises(SchemaError, match="orphan SQLite sidecars"):
        migrate_store_copy(
            orphan_db,
            tmp_path / "orphan_dst" / "progress.db",
            store="progress",
            allow_missing_source=True,
            scratch_dir=tmp_path / "scratch_orphan",
        )

    # 3) Corrupt source fails PRAGMA integrity_check and unlinks the partial destination file.
    corrupt_dir = tmp_path / "corrupt_src"
    corrupt_dir.mkdir()
    corrupt_db = corrupt_dir / "progress.db"
    _build_progress_v0(corrupt_db, "v0_1a35a1a")
    raw = bytearray(corrupt_db.read_bytes())
    raw[-128:] = b"\xff" * 128
    corrupt_db.write_bytes(bytes(raw))
    corrupt_dst = tmp_path / "corrupt_dst" / "progress.db"
    with pytest.raises((SchemaError, sqlite3.DatabaseError)):
        migrate_store_copy(
            corrupt_db, corrupt_dst, store="progress", scratch_dir=tmp_path / "scratch_corrupt"
        )
    assert not corrupt_dst.exists()

    # 4) A failure during SQLite backup unlinks the destination file cleanly.
    backup_dst = tmp_path / "backup_fail_dst" / "progress.db"

    class _FailingBackupConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, *args, **kwargs):
            return self._inner.execute(*args, **kwargs)

        def backup(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("simulated backup I/O failure")

        def close(self):
            self._inner.close()

    real_connect = sqlite3.connect
    calls = {"n": 0}

    def wrapped_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            return _FailingBackupConn(conn)
        return conn

    monkeypatch.setattr("sigint101.migrations.sqlite3.connect", wrapped_connect)
    with pytest.raises(sqlite3.OperationalError, match="simulated backup I/O failure"):
        migrate_store_copy(
            src, backup_dst, store="progress", scratch_dir=tmp_path / "scratch_backup_fail"
        )
    assert not backup_dst.exists()
    monkeypatch.undo()

    # 5) R2-a: scratch inside/equal to source dir or an ancestor of destination is refused.
    nested_dst = tmp_path / "outer_scratch" / "gen" / "progress.db"
    for bad_scratch in (
        src_dir,
        src_dir / "nested_scratch",
        tmp_path / "outer_scratch",
    ):
        with pytest.raises(ValueError, match="scratch"):
            migrate_store_copy(src, nested_dst, store="progress", scratch_dir=bad_scratch)


def test_storage_normalize_sql_and_open_error_and_sidecar_paths(tmp_path, monkeypatch):
    # 1) _normalize_sql comment, bracket-identifier, doubled-quote, and trailing-semicolon paths.
    assert storage_mod._normalize_sql(None) == ""
    assert storage_mod._normalize_sql("") == ""
    sql = (
        "CREATE TABLE [MyTbl] (\n"
        "  -- leading line comment\n"
        "  id INTEGER PRIMARY KEY, /* block comment */ "
        "label TEXT DEFAULT 'can''t'/*inline*/,\n"
        "  \"col\"\"name\" TEXT--tail comment\n"
        ");;;"
    )
    norm = storage_mod._normalize_sql(sql)
    expected_norm = (
        "create table [MyTbl] (id integer primary key, "
        "label text default 'can''t', \"col\"\"name\" text)"
    )
    assert norm == expected_norm
    # Unclosed bracket identifier stays intact without crashing.
    assert storage_mod._normalize_sql("CREATE TABLE [unclosed") == "create table [unclosed"

    # 2) sync_media_asset drops the dead sidecar_path parameter.
    import inspect

    assert list(inspect.signature(storage_mod.sync_media_asset).parameters) == ["png_path"]

    # 3) Header read OSError and sqlite3.connect sqlite3.Error raise SchemaError.
    gen = tmp_path / "err_gen"
    gen.mkdir()
    db_path = gen / "progress.db"
    migrate_store_copy(
        tmp_path / "none.db",
        db_path,
        store="progress",
        allow_missing_source=True,
        scratch_dir=tmp_path / "scratch_err",
    )
    sig = reviewed_signatures("progress")["v1"]

    def bad_open(*_args, **_kwargs):
        raise OSError("unreadable header")

    monkeypatch.setattr(Path, "open", bad_open)
    with pytest.raises(SchemaError, match="cannot read managed store"):
        open_managed_sqlite(db_path, expected_version=1, expected_signature=sig)
    monkeypatch.undo()

    def bad_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated connect failure")

    monkeypatch.setattr("sigint101.storage.sqlite3.connect", bad_connect)
    with pytest.raises(SchemaError, match="cannot open managed store"):
        open_managed_sqlite(db_path, expected_version=1, expected_signature=sig)
    monkeypatch.undo()

    # 4) R2-b: stray -wal or -shm beside a DELETE-mode store is refused without touching disk.
    for ext in ("-wal", "-shm"):
        stray = Path(f"{db_path}{ext}")
        stray.write_bytes(b"stray-sidecar-bytes")
        before_db = db_path.read_bytes()
        try:
            with pytest.raises(SchemaError, match="WAL|sidecar"):
                open_managed_sqlite(db_path, expected_version=1, expected_signature=sig)
            assert db_path.read_bytes() == before_db
            assert stray.read_bytes() == b"stray-sidecar-bytes"
        finally:
            stray.unlink()

    # 5) F14 residual: read-only open of a hot-journal store raises SchemaError.
    hot_dir = tmp_path / "hot_ro"
    hot_dir.mkdir()
    hot_db = hot_dir / "progress.db"
    shutil.copy2(db_path, hot_db)
    conn = sqlite3.connect(hot_db, isolation_level=None)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA cache_size = 1")
    conn.execute("BEGIN EXCLUSIVE")
    for idx in range(100, 140):
        conn.execute(
            "INSERT INTO progress(lesson_id, opened, completed, updated) VALUES(?, 1, 0, 1.0)",
            (idx,),
        )
    ro_snap_dir = tmp_path / "hot_ro_snap"
    ro_snap_dir.mkdir()
    ro_db = ro_snap_dir / "progress.db"
    shutil.copy2(hot_db, ro_db)
    shutil.copy2(Path(f"{hot_db}-journal"), Path(f"{ro_db}-journal"))
    conn.execute("ROLLBACK")
    conn.close()

    before_ro_db = ro_db.read_bytes()
    before_ro_journal = Path(f"{ro_db}-journal").read_bytes()
    with pytest.raises(SchemaError, match="read-only|journal|cannot"):
        open_managed_sqlite(ro_db, expected_version=1, expected_signature=sig, read_only=True)
    assert ro_db.read_bytes() == before_ro_db
    assert Path(f"{ro_db}-journal").read_bytes() == before_ro_journal


def test_apt_report_skips_announcement_when_sync_fails(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    from sigint101.apt import AptDecoder

    apt_dir = tmp_path / "apt"
    apt_dir.mkdir()
    first = apt_dir / "pass1.png"
    second = apt_dir / "pass2.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    second.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
    os.utime(first, (1_700_000_001, 1_700_000_001))
    os.utime(second, (1_700_000_002, 1_700_000_002))

    decoder = AptDecoder(output_dir=apt_dir, data_dir=tmp_path, sync_assets=True)
    emitted: list[str] = []
    decoder.on_reception = lambda item, **_kw: emitted.append(item["path"])

    def selective_sync(png_path):
        if Path(png_path).name == "pass1.png":
            raise OSError("simulated fsync EIO on pass1")

    monkeypatch.setattr("sigint101.apt.sync_media_asset", selective_sync)
    run = SimpleNamespace(
        args=(137_100_000, set(), {"kind": "rtl-sdr", "serial": None, "index": 0}),
        proc=SimpleNamespace(args=["satdump"]),
        ready=threading.Event(),
    )
    decoder._report(run)
    assert run.ready.is_set()
    assert emitted == ["apt/pass2.png"]
    assert "simulated fsync EIO on pass1" in (decoder.error or "")
