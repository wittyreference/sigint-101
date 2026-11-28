# ABOUTME: Numbered SQLite migrations (v0 -> v1) on private copies, backed by reviewed schema
# ABOUTME: fixtures from git history. Never mutates source DBs; rolls back atomically on error.

from __future__ import annotations

import functools
import json
import os
import shutil
import sqlite3
from pathlib import Path

from .storage import SchemaError, fsync_dir, fsync_file, schema_signature

PROGRESS_SCHEMA_VERSION = 1
EXAM_SCHEMA_VERSION = 1
STORES = ("progress", "exam")

FIXTURES_PATH = Path(__file__).with_name("schema_fixtures.json")

_SQLITE_SIDECARS = ("-journal", "-wal", "-shm")

_PROGRESS_TABLE_DDL: tuple[tuple[str, str], ...] = (
    (
        "progress",
        "CREATE TABLE progress ("
        "lesson_id INTEGER PRIMARY KEY, "
        "opened INTEGER NOT NULL DEFAULT 0, "
        "completed INTEGER NOT NULL DEFAULT 0, "
        "updated REAL)",
    ),
    (
        "responses",
        "CREATE TABLE responses ("
        "lesson_id INTEGER NOT NULL, "
        "kind TEXT NOT NULL, "
        "body TEXT NOT NULL, "
        "updated REAL, "
        "correct INTEGER, "
        "PRIMARY KEY (lesson_id, kind))",
    ),
    (
        "stages",
        "CREATE TABLE stages ("
        "lesson_id INTEGER PRIMARY KEY, "
        "stage TEXT NOT NULL, "
        "furthest INTEGER NOT NULL DEFAULT 0, "
        "updated REAL)",
    ),
    (
        "places",
        "CREATE TABLE places ("
        "name TEXT PRIMARY KEY, "
        "value TEXT NOT NULL, "
        "updated REAL)",
    ),
    (
        "stamps",
        "CREATE TABLE stamps ("
        "instrument TEXT PRIMARY KEY, "
        "first_ts REAL NOT NULL, "
        "count INTEGER NOT NULL, "
        "provenance TEXT NOT NULL, "
        "digest TEXT NOT NULL)",
    ),
    (
        "stamp_events",
        "CREATE TABLE stamp_events (digest BLOB PRIMARY KEY) WITHOUT ROWID",
    ),
    (
        "sort_answers",
        "CREATE TABLE sort_answers ("
        "lesson_id INTEGER NOT NULL, "
        "card_key TEXT NOT NULL, "
        "choice TEXT NOT NULL, "
        "correct INTEGER NOT NULL, "
        "updated REAL, "
        "PRIMARY KEY (lesson_id, card_key))",
    ),
)

_EXAM_TABLE_DDL: tuple[tuple[str, str], ...] = (
    (
        "srs",
        "CREATE TABLE srs ("
        "question_id TEXT PRIMARY KEY, "
        "box INTEGER NOT NULL DEFAULT 1, "
        "last_seen REAL, "
        "seen INTEGER NOT NULL DEFAULT 0, "
        "correct INTEGER NOT NULL DEFAULT 0)",
    ),
    (
        "sim",
        "CREATE TABLE sim ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "question_ids TEXT NOT NULL, "
        "answers TEXT NOT NULL DEFAULT '{}', "
        "created REAL, "
        "skipped TEXT NOT NULL DEFAULT '[]')",
    ),
)


@functools.lru_cache(maxsize=1)
def _all_fixtures() -> dict:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def reviewed_signatures(store: str) -> dict:
    """Return the frozen reviewed v0 variants and v1 target signature for `store`."""
    if store not in STORES:
        raise ValueError(f"unknown store: {store!r}")
    return _all_fixtures()[store]


def match_v0_signature(store: str, sig: dict) -> str | None:
    """Return the exact reviewed version-0 variant name matching `sig`, or None."""
    variants = reviewed_signatures(store)["v0"]
    for name, expected in variants.items():
        if sig == expected:
            return name
    return None


def _target_head(store: str) -> int:
    if store == "progress":
        return PROGRESS_SCHEMA_VERSION
    if store == "exam":
        return EXAM_SCHEMA_VERSION
    raise ValueError(f"unknown store: {store!r}")


def _validate_scratch_dir(dest_path: Path, scratch_dir: Path) -> Path:
    """Require an exclusive private scratch directory outside the destination generation."""
    dest_gen = dest_path.parent.resolve()
    scratch = scratch_dir.resolve()
    if scratch == dest_gen or scratch.is_relative_to(dest_gen):
        raise ValueError(
            f"recovery scratch directory must stay outside destination generation {dest_gen}: "
            f"{scratch}"
        )
    if scratch.exists():
        raise FileExistsError(f"recovery scratch directory already exists: {scratch}")
    return scratch


def _copy_sqlite_to_standalone_delete(
    source_path: Path,
    dest_path: Path,
    *,
    scratch_dir: Path,
    allow_missing_source: bool,
) -> None:
    """Copy a quiesced SQLite DB (recovering any journal/WAL in scratch_dir) to dest_path.

    Recovery scratch is created exclusively outside the destination generation and left with the
    operation's records — never placed inside the generation and never recursively deleted.
    """
    scratch = _validate_scratch_dir(dest_path, scratch_dir)
    if dest_path.exists():
        raise FileExistsError(f"migration destination already exists: {dest_path}")

    sidecars = [Path(f"{source_path}{ext}") for ext in _SQLITE_SIDECARS
                if Path(f"{source_path}{ext}").exists()]
    if not source_path.exists():
        if sidecars:
            raise SchemaError(
                f"orphan SQLite sidecars exist without main database: {source_path.name}"
            )
        if not allow_missing_source:
            raise SchemaError(f"source database is missing: {source_path.name}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(os.fspath(dest_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        conn = sqlite3.connect(dest_path)
        try:
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.commit()
        finally:
            conn.close()
        return

    # Copy source + sidecars into the caller-supplied private scratch dir with writable 0600 modes
    # so hot-journal or WAL recovery succeeds even on a 0444/0555 read-only source, without ever
    # touching the source files or writing scratch inside the destination generation.
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(mode=0o700, exist_ok=False)
    scratch_src = scratch / source_path.name
    shutil.copyfile(source_path, scratch_src)
    scratch_src.chmod(0o600)
    for sidecar in sidecars:
        scratch_sidecar = scratch / sidecar.name
        shutil.copyfile(sidecar, scratch_sidecar)
        scratch_sidecar.chmod(0o600)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(os.fspath(dest_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)

    src_conn = sqlite3.connect(scratch_src)
    dst_conn = sqlite3.connect(dest_path)
    try:
        check = src_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise SchemaError(f"source database failed integrity_check: {check}")
        src_conn.backup(dst_conn)
        dst_conn.execute("PRAGMA journal_mode = DELETE")
        dst_conn.commit()
    except BaseException:
        dst_conn.close()
        if dest_path.exists():
            dest_path.unlink()
        raise
    finally:
        src_conn.close()
    dst_conn.close()


def _snapshot_rows(conn: sqlite3.Connection, tables: dict) -> dict[str, tuple[list[str], list]]:
    """Capture every row of every existing table using SQLite quote() for type-exact equality."""
    snap: dict[str, tuple[list[str], list]] = {}
    for table, info in tables.items():
        cols = [col[1] for col in info["columns"]]
        quote_sql = ", ".join(f'quote("{c}")' for c in cols)
        order_sql = ", ".join(f'"{c}"' for c in cols)
        rows = conn.execute(
            f'SELECT {quote_sql} FROM "{table}" ORDER BY {order_sql}, {quote_sql}'
        ).fetchall()
        snap[table] = (cols, rows)
    return snap


def _verify_preserved_rows(
    conn: sqlite3.Connection,
    before: dict[str, tuple[list[str], list]],
) -> None:
    for table, (cols, expected_rows) in before.items():
        quote_sql = ", ".join(f'quote("{c}")' for c in cols)
        order_sql = ", ".join(f'"{c}"' for c in cols)
        actual = conn.execute(
            f'SELECT {quote_sql} FROM "{table}" ORDER BY {order_sql}, {quote_sql}'
        ).fetchall()
        if actual != expected_rows:
            raise SchemaError(f"migration altered pre-existing rows in table {table!r}")


def _migration_steps_v0_to_v1(store: str, sig: dict) -> list[str]:
    existing = sig["tables"]
    steps: list[str] = []
    if store == "progress":
        for table, ddl in _PROGRESS_TABLE_DDL:
            if table not in existing:
                steps.append(ddl)
            elif table == "responses":
                col_names = {c[1] for c in existing["responses"]["columns"]}
                if "correct" not in col_names:
                    steps.append("ALTER TABLE responses ADD COLUMN correct INTEGER")
    elif store == "exam":
        for table, ddl in _EXAM_TABLE_DDL:
            if table not in existing:
                steps.append(ddl)
            elif table == "sim":
                col_names = {c[1] for c in existing["sim"]["columns"]}
                if "skipped" not in col_names:
                    steps.append(
                        "ALTER TABLE sim ADD COLUMN skipped TEXT NOT NULL DEFAULT '[]'"
                    )
    else:
        raise ValueError(f"unknown store: {store!r}")
    return steps


def migrate_store_copy(
    source_path: str | Path,
    dest_path: str | Path,
    *,
    store: str,
    scratch_dir: str | Path,
    allow_missing_source: bool = False,
    _after_step_hook=None,
    _after_version_hook=None,
) -> dict:
    """Copy `source_path` to an exclusive `dest_path` and migrate v0 -> v1 in one transaction.

    Never writes to `source_path` and never places recovery scratch inside `dest_path.parent`.
    Executes every DDL step and `PRAGMA user_version=1` inside one explicit `BEGIN IMMEDIATE`
    transaction (never `executescript`), verifies schema and preserved rows before commit,
    reopens to confirm integrity and head, and fsyncs dest + parent.
    """
    target_head = _target_head(store)
    fixtures = reviewed_signatures(store)
    expected_v1 = fixtures["v1"]
    src = Path(source_path)
    dst = Path(dest_path)

    _copy_sqlite_to_standalone_delete(
        src, dst, scratch_dir=Path(scratch_dir), allow_missing_source=allow_missing_source
    )

    conn = sqlite3.connect(dst, isolation_level=None)
    matched_v0 = None
    migrated = False
    try:
        from_version = conn.execute("PRAGMA user_version").fetchone()[0]
        sig = schema_signature(conn)

        if from_version == target_head:
            if sig != expected_v1:
                raise SchemaError(
                    f"unrecognized {store} schema at head {target_head}: "
                    f"tables={sorted(sig['tables'])}"
                )
        elif from_version != 0:
            raise SchemaError(
                f"unsupported {store} schema head {from_version} (expected 0 or {target_head})"
            )
        else:
            matched_v0 = match_v0_signature(store, sig)
            if matched_v0 is None:
                raise SchemaError(
                    f"unrecognized {store} version-0 schema: tables={sorted(sig['tables'])}"
                )
            before_rows = _snapshot_rows(conn, sig["tables"])
            steps = _migration_steps_v0_to_v1(store, sig)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in steps:
                    conn.execute(stmt)
                    if _after_step_hook is not None:
                        _after_step_hook(conn)
                conn.execute(f"PRAGMA user_version = {target_head}")
                if _after_version_hook is not None:
                    _after_version_hook(conn)
                if schema_signature(conn) != expected_v1:
                    raise SchemaError(f"{store} post-migration schema did not match v1 target")
                _verify_preserved_rows(conn, before_rows)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            migrated = True
    finally:
        conn.close()

    # Reopen independently to verify integrity, rollback-journal mode, head, and v1 signature.
    verify = sqlite3.connect(dst)
    try:
        check = verify.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise SchemaError(f"migrated {store} failed integrity_check: {check}")
        jmode = str(verify.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if jmode != "delete":
            raise SchemaError(f"migrated {store} must be in journal_mode=delete, got {jmode!r}")
        head = verify.execute("PRAGMA user_version").fetchone()[0]
        if head != target_head:
            raise SchemaError(f"migrated {store} head is {head}, expected {target_head}")
        if schema_signature(verify) != expected_v1:
            raise SchemaError(f"reopened {store} schema does not match v1 target")
    finally:
        verify.close()

    fsync_file(dst)
    fsync_dir(dst.parent)
    return {
        "store": store,
        "from_version": from_version,
        "to_version": target_head,
        "matched_v0": matched_v0,
        "migrated": migrated,
    }
