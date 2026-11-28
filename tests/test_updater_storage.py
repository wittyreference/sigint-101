# ABOUTME: Tests PR 1 of the updater plan: schema fixtures, numbered migrations, managed open,
# ABOUTME: durability/acknowledgement fences, live SQLite PRAGMAs, and R2-1/R2-4 build rules.

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101 import events as events_mod
from sigint101 import storage as storage_mod
from sigint101.app import create_app
from sigint101.apt import AptDecoder
from sigint101.exam import ExamStore
from sigint101.hardware import RadioProbe
from sigint101.migrations import (
    EXAM_SCHEMA_VERSION,
    PROGRESS_SCHEMA_VERSION,
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
    schema_signature,
    validate_updater_root,
    verify_managed_connection,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _absent():
    return RadioProbe(False, "lsusb", "no RTL2832U on USB bus")


# --- Historical v0 builders matching main's real git history -----------------------------------


def _build_progress_v0(path: Path, variant: str) -> dict:
    """Create a real historical version-0 progress.db fixture and return seeded expected rows."""
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
    assert variant == "v0_1a35a1a"
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
    ["empty", "v0_72ddf9c", "v0_02c2095", "v0_e72c706", "v0_1a35a1a"],
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

    rp = migrate_store_copy(src_p, dst_p, store="progress")
    re_ = migrate_store_copy(src_e, dst_e, store="exam")

    # Source files are NEVER touched.
    assert src_p.read_bytes() == orig_p_bytes
    assert src_e.read_bytes() == orig_e_bytes

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
    if p_variant in ("v0_e72c706", "v0_1a35a1a"):
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
    dst_p2 = tmp_path / "dst2_progress.db"
    dst_e2 = tmp_path / "dst2_exam.db"
    rp2 = migrate_store_copy(dst_p, dst_p2, store="progress")
    re2 = migrate_store_copy(dst_e, dst_e2, store="exam")
    assert rp2["migrated"] is False and rp2["from_version"] == 1 and rp2["to_version"] == 1
    assert re2["migrated"] is False and re2["from_version"] == 1 and re2["to_version"] == 1


def test_missing_legacy_store_bootstraps_v1_only_when_explicitly_allowed(tmp_path):
    missing = tmp_path / "absent.db"
    dst = tmp_path / "bootstrapped.db"
    with pytest.raises(SchemaError, match="missing"):
        migrate_store_copy(missing, dst, store="progress", allow_missing_source=False)
    assert not dst.exists()

    receipt = migrate_store_copy(missing, dst, store="progress", allow_missing_source=True)
    assert receipt["from_version"] == 0 and receipt["to_version"] == 1
    assert not missing.exists()
    prog = Progress(dst, managed=True)
    assert prog.stamps() == []
    prog.close()


# --- §8 Test 2: failed migration rolls back schema, rows, and user_version --------------------


@pytest.mark.parametrize("store", ["progress", "exam"])
@pytest.mark.parametrize("fail_point", ["after_step", "after_version"])
def test_failed_migration_rolls_back_schema_rows_and_version(tmp_path, store, fail_point):
    src = tmp_path / f"src_{store}.db"
    dst = tmp_path / f"dst_{store}.db"
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
        migrate_store_copy(src, dst, store=store, **kwargs)

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
    src = tmp_path / f"bad_{store}_{bad_kind}.db"
    dst = tmp_path / f"out_{store}_{bad_kind}.db"
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
        migrate_store_copy(src, dst, store=store)

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
    stale = tmp_path / f"stale_{store}.db"
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
    migrated = tmp_path / f"v1_{store}.db"
    migrate_store_copy(stale, migrated, store=store)
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
                       store="progress", allow_missing_source=True)
    migrate_store_copy(tmp_path / "m_e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True)

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

    # 3) Completed media assets (SSTV PNG + JSON sidecar + dir, and APT PNG + dir):
    # restore working spies, verify file+sidecar+dir sync order, and verify failure blocks ack.
    media_calls: list[tuple[str, str]] = []
    real_fsync_file = storage_mod.fsync_file

    def spy_media_file(path):
        media_calls.append(("file", Path(path).name))
        real_fsync_file(path)

    def spy_media_dir(path):
        media_calls.append(("dir", Path(path).name))
        real_fsync_dir(path)

    monkeypatch.setattr(storage_mod, "fsync_file", spy_media_file)
    monkeypatch.setattr(storage_mod, "fsync_dir", spy_media_dir)

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
        ("file", published.name),
        ("file", published.with_suffix(".json").name),
        ("dir", "sstv"),
    ]

    # Inject media sync failure -> _publish_capture must return False (blocks emission/ack).
    def fail_media_file(_path):
        raise OSError("media fsync failed")

    monkeypatch.setattr(storage_mod, "fsync_file", fail_media_file)
    staged2 = sstv_dir / ".staging" / "cap2.png"
    staged2.write_bytes(b"\x89PNG\r\n\x1a\nfake2")
    published2 = sstv_dir / "20261128_000002_martin1.png"
    cap2 = _Capture(staged_path=staged2, published_path=published2, freq_hz=145_800_000,
                    decoded_at=101.0, mode="Martin 1")
    assert sstv._publish_capture(cap2) is False
    assert sstv.error and "media fsync failed" in sstv.error

    # APT media publication sync fence: restores working spy, verifies sync before emit, then
    # injects failure and verifies emit is blocked.
    monkeypatch.setattr(storage_mod, "fsync_file", spy_media_file)
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
                       store="progress", allow_missing_source=True)
    migrate_store_copy(tmp_path / "none_e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True)

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

    # Prove the CI guard catches dropping either stamp table or adding an unreviewed table.
    for drop_table in ("stamps", "stamp_events", "sort_answers"):
        mutated = copy.deepcopy(p_sig)
        del mutated["tables"][drop_table]
        if drop_table == "stamps":
            # Missing stamps while keeping stamp_events is an unreviewed partial schema.
            assert match_v0_signature("progress", mutated) is None
        elif drop_table == "stamp_events":
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
    pre_134 = tmp_path / "pre134.db"
    _build_progress_v0(pre_134, "v0_02c2095")
    pre_out = tmp_path / "pre134_v1.db"
    migrate_store_copy(pre_134, pre_out, store="progress")
    p_pre = Progress(pre_out, managed=True)
    assert p_pre.stamps() == []
    assert p_pre.get(1)["completed"] == 1
    p_pre.close()

    pr_134 = tmp_path / "pr134.db"
    _build_progress_v0(pr_134, "v0_e72c706")
    pr_out = tmp_path / "pr134_v1.db"
    migrate_store_copy(pr_134, pr_out, store="progress")
    p_134 = Progress(pr_out, managed=True)
    stamps = p_134.stamps()
    assert len(stamps) == 1 and stamps[0]["instrument"] == "ism" and stamps[0]["count"] == 3
    p_134.close()


# --- WAL recovery on private copy + R2-4 root check + R2-1 routine site settings --------------


def test_wal_source_is_recovered_on_private_copy_without_mutating_source(tmp_path):
    import shutil

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
    src = tmp_path / "wal_progress.db"
    shutil.copy2(live, src)
    shutil.copy2(Path(f"{live}-wal"), Path(f"{src}-wal"))
    if Path(f"{live}-shm").exists():
        shutil.copy2(Path(f"{live}-shm"), Path(f"{src}-shm"))
    conn.close()

    wal_path = Path(f"{src}-wal")
    assert wal_path.exists() and wal_path.stat().st_size > 0
    src_before = src.read_bytes()
    wal_before = wal_path.read_bytes()

    dst = tmp_path / "recovered_v1.db"
    receipt = migrate_store_copy(src, dst, store="progress")

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
    with pytest.raises(ValueError, match="local/share/sigint101"):
        validate_updater_root(forbidden, home=fake_home)
    with pytest.raises(ValueError, match="local/share/sigint101"):
        validate_updater_root(forbidden / "nested", home=fake_home)
    with pytest.raises(ValueError, match="absolute"):
        validate_updater_root(Path("relative/root"), home=fake_home)

    allowed = fake_home / DEFAULT_UPDATER_ROOT_REL
    assert validate_updater_root(allowed, home=fake_home) == allowed.resolve()


def test_r2_1_routine_managed_boot_allows_documented_site_setting_changes(tmp_path, monkeypatch):
    # R2-1: settings parity applies only before an operation's serving marker and only to the
    # unit's own settings; changing a documented site setting never refuses a routine managed boot.
    gen_dir = tmp_path / "gen"
    gen_dir.mkdir()
    migrate_store_copy(tmp_path / "p.db", gen_dir / "progress.db",
                       store="progress", allow_missing_source=True)
    migrate_store_copy(tmp_path / "e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True)

    for serial in ("00000001", "00000002"):
        monkeypatch.setenv("SIGINT101_RTLSDR_SERIAL", serial)
        app = create_app(data_dir=gen_dir, probe=_absent, managed=True)
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
