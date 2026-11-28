# ABOUTME: On-disk contract helpers for managed SIGINT 101 stores: durability fences, updater root
# ABOUTME: boundary checks (R2-4), canonical SQLite schema signatures, and managed no-DDL opening.

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

# R2-4: deploy/desktop/apply-theme.sh writes wallpaper.png into ~/.local/share/sigint101, so the
# updater's private root must never sit there or beneath it.
FORBIDDEN_ROOT_REL = Path(".local") / "share" / "sigint101"
DEFAULT_UPDATER_ROOT_REL = Path(".local") / "state" / "sigint101-updater"

_CHECK_RE = re.compile(r"CHECK\s*\(([^()]+)\)", re.IGNORECASE)


class SchemaError(RuntimeError):
    """Raised when a store is missing, has an unreviewed schema/head, or violates managed rules."""


def validate_updater_root(root: str | Path, *, home: str | Path | None = None) -> Path:
    """Validate an absolute updater ROOT path and refuse ~/.local/share/sigint101 (R2-4)."""
    root_path = Path(root)
    if not root_path.is_absolute():
        raise ValueError(f"updater root must be an absolute path: {root_path}")
    resolved = root_path.resolve()
    home_dir = Path(home).resolve() if home is not None else Path.home().resolve()
    forbidden = (home_dir / FORBIDDEN_ROOT_REL).resolve()
    if resolved == forbidden or resolved.is_relative_to(forbidden):
        raise ValueError(
            f"updater root must not be {FORBIDDEN_ROOT_REL} or inside it: {resolved}"
        )
    return resolved


def fsync_handle(handle) -> None:
    """Flush Python buffers and fsync an open file handle to durable storage."""
    handle.flush()
    os.fsync(handle.fileno())


def fsync_file(path: str | Path) -> None:
    """Fsync a regular file by path without modifying its bytes or timestamps."""
    fd = os.open(os.fspath(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: str | Path) -> None:
    """Fsync a directory so file creation or rename entries survive power loss."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(os.fspath(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_media_asset(png_path: str | Path, *, sidecar_path: str | Path | None = None) -> None:
    """Fsync a published media PNG, its optional metadata sidecar, and their directory."""
    png = Path(png_path)
    fsync_file(png)
    if sidecar_path is not None:
        sidecar = Path(sidecar_path)
        if sidecar.exists():
            fsync_file(sidecar)
    fsync_dir(png.parent)


def _normalized_checks(sql: str | None) -> list[str]:
    if not sql:
        return []
    checks = []
    for match in _CHECK_RE.finditer(sql):
        expr = re.sub(r"\s+", "", match.group(1)).lower()
        checks.append(f"check({expr})")
    checks.sort()
    return checks


def schema_signature(conn: sqlite3.Connection) -> dict:
    """Return a canonical structural signature of every table, index, constraint, trigger, view."""
    tables: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for row in rows:
        name, sql = row[0], row[1]
        cols = [
            [r[0], r[1], r[2], r[3], r[4], r[5]]
            for r in conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        ]
        idxs = []
        for idx in conn.execute(f'PRAGMA index_list("{name}")').fetchall():
            idx_cols = [
                [c[0], c[1], c[2]]
                for c in conn.execute(f'PRAGMA index_info("{idx[1]}")').fetchall()
            ]
            idxs.append({
                "unique": idx[2],
                "origin": idx[3],
                "partial": idx[4],
                "cols": idx_cols,
            })
        idxs.sort(key=lambda item: (item["unique"], item["origin"], item["partial"], item["cols"]))
        fks = [
            list(fk) for fk in conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
        ]
        tables[name] = {
            "columns": cols,
            "indexes": idxs,
            "without_rowid": "without rowid" in (sql or "").lower(),
            "checks": _normalized_checks(sql),
            "foreign_keys": fks,
        }

    triggers = [
        [r[0], r[1], " ".join((r[2] or "").split())]
        for r in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).fetchall()
    ]
    views = [
        [r[0], " ".join((r[1] or "").split())]
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name"
        ).fetchall()
    ]
    return {"tables": tables, "triggers": triggers, "views": views}


def verify_managed_connection(
    conn: sqlite3.Connection,
    *,
    expected_version: int,
    expected_signature: dict,
    read_only: bool = False,
) -> None:
    """Assert exact SQLite durability PRAGMAs, schema head, and structural signature."""
    jmode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if jmode != "delete":
        raise SchemaError(f"managed store requires journal_mode=delete, found {jmode!r}")
    if not read_only:
        sync_val = conn.execute("PRAGMA synchronous").fetchone()[0]
        if sync_val != 3:
            raise SchemaError(
                f"managed store requires synchronous=3 (EXTRA), found {sync_val!r}"
            )
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != expected_version:
        raise SchemaError(
            f"managed store schema version is {version}, expected {expected_version}; "
            "migrate before opening"
        )
    actual_sig = schema_signature(conn)
    if actual_sig != expected_signature:
        raise SchemaError("managed store schema does not match the reviewed target signature")


def open_managed_sqlite(
    db_path: str | Path,
    *,
    expected_version: int,
    expected_signature: dict,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open an existing migrated SQLite store with no DDL, enforcing DELETE + EXTRA and head."""
    path = Path(db_path)
    if not path.is_file():
        raise SchemaError(f"managed store file is missing: {path.name}")
    mode = "ro" if read_only else "rw"
    uri = f"{path.resolve().as_uri()}?mode={mode}"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as exc:
        raise SchemaError(f"cannot open managed store {path.name}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        # Read journal_mode before issuing any PRAGMA write so an unexpected WAL file is refused
        # without ever touching a byte on disk.
        jmode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if jmode != "delete":
            raise SchemaError(f"managed store requires journal_mode=delete, found {jmode!r}")
        if not read_only:
            conn.execute("PRAGMA synchronous = EXTRA")
        verify_managed_connection(
            conn,
            expected_version=expected_version,
            expected_signature=expected_signature,
            read_only=read_only,
        )
    except BaseException:
        conn.close()
        raise
    return conn
