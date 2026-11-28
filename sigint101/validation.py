# ABOUTME: Exact open-pair attestation, course/saved-state validation, and systemd unit settings
# ABOUTME: loading/parsing + R2-1 parity fingerprint checks for private read-only updater trials.

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError

import sigint101

from .exam import LEITNER_BOXES, POOL_PATH, ExamStore, Pool
from .lessons import CHECK_STAGE, SORT_ANSWERS, Lesson
from .migrations import EXAM_SCHEMA_VERSION, PROGRESS_SCHEMA_VERSION, reviewed_signatures
from .operate import INSTRUMENTS
from .progress import CHECK, PREDICTION, Progress
from .retrieval import MAX_BOX as RETRIEVAL_MAX_BOX
from .snapshot import GENERATION_MARKER_FILENAME
from .stamps import STAMP_INSTRUMENTS
from .unit_settings import (
    ALLOWED_DROPIN_DIRECTIVES,
    FORBIDDEN_UNIT_ENV_PREFIXES,
    FORBIDDEN_UNIT_ENV_VARS,
    SUPPORTED_UNIT_DIRECTIVES,
    UnitSettings,
    UnitSettingsError,
    check_unit_settings_parity,
    compute_settings_fingerprint,
    is_forbidden_trial_env_var,
    load_systemd_unit_settings,
    parse_systemd_unit_config,
)
from .units import unit_for_lesson

RELEASE_MANIFEST_FILENAME = "release.json"

# Explicit saved-state reference coverage registries: every reviewed v1 table
# must appear in either CHECKED_*_TABLES or NO_REFERENCE_*_TABLES (never silently unvalidated).
CHECKED_PROGRESS_TABLES: frozenset[str] = frozenset(
    {
        "progress",
        "stages",
        "responses",
        "sort_answers",
        "places",
        "stamps",
        "retrieval",
        "challenges",
        "challenge_passes",
        "replays_seen",
    }
)
NO_REFERENCE_PROGRESS_TABLES: frozenset[str] = frozenset({"stamp_events"})
CHECKED_EXAM_TABLES: frozenset[str] = frozenset({"srs", "sim"})
NO_REFERENCE_EXAM_TABLES: frozenset[str] = frozenset()

# Shipped lesson IDs required by default when validating a full course pack directory.
SHIPPED_REQUIRED_LESSONS: frozenset[int] = frozenset(range(1, 30)) | {39}

REQUIRED_TEMPLATES: tuple[str, ...] = (
    "base.html",
    "home.html",
    "unit.html",
    "console.html",
    "operate.html",
    "law.html",
    "logbook.html",
    "exam.html",
    "exam_sim.html",
    "exam_results.html",
    "error.html",
    "_panels.html",
    "_tilegrid.html",
    "_exam_figure.html",
)

__all__ = [
    "ALLOWED_DROPIN_DIRECTIVES",
    "CHECKED_EXAM_TABLES",
    "CHECKED_PROGRESS_TABLES",
    "FORBIDDEN_UNIT_ENV_PREFIXES",
    "FORBIDDEN_UNIT_ENV_VARS",
    "NO_REFERENCE_EXAM_TABLES",
    "NO_REFERENCE_PROGRESS_TABLES",
    "RELEASE_MANIFEST_FILENAME",
    "REQUIRED_TEMPLATES",
    "SHIPPED_REQUIRED_LESSONS",
    "SUPPORTED_UNIT_DIRECTIVES",
    "UnitSettings",
    "UnitSettingsError",
    "ValidationError",
    "attest_open_pair",
    "check_saved_state_table_coverage",
    "check_unit_settings_parity",
    "compute_settings_fingerprint",
    "is_forbidden_trial_env_var",
    "load_systemd_unit_settings",
    "open_sqlite_identity",
    "parse_systemd_unit_config",
    "read_generation_marker",
    "read_release_manifest",
    "resolve_release_pool_path",
    "validate_course_content",
    "validate_saved_state",
    "verify_pair_attestation",
    "write_release_manifest",
]


class ValidationError(RuntimeError):
    """Raised when an open release/data pair, course asset, or saved-state row fails validation."""


def write_release_manifest(
    release_root: str | Path,
    *,
    release_id: str,
    commit: str,
) -> Path:
    """Write a canonical release.json manifest at the root of a candidate release tree."""
    root = Path(release_root)
    payload = {"release_id": release_id, "commit": commit}
    manifest_path = root / RELEASE_MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def read_release_manifest(package_root: str | Path) -> dict:
    """Read and validate release.json from an imported package's actual root directory."""
    root = Path(package_root).resolve()
    manifest_path = root / RELEASE_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ValidationError(
            f"release manifest is missing at {root.name}/{RELEASE_MANIFEST_FILENAME}"
        )
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"release manifest cannot be parsed: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("release manifest must be a JSON object")
    release_id = data.get("release_id")
    commit = data.get("commit")
    if not isinstance(release_id, str) or not release_id.strip():
        raise ValidationError("release manifest is missing a valid release_id")
    if not isinstance(commit, str) or not commit.strip():
        raise ValidationError("release manifest is missing a valid commit")
    st = os.stat(root)
    return {
        "release_id": release_id,
        "commit": commit,
        "package_root": str(root),
        "st_dev": st.st_dev,
        "st_ino": st.st_ino,
    }


def read_generation_marker(data_dir: str | Path) -> dict:
    """Read and validate generation.json from the open data generation directory."""
    root = Path(data_dir).resolve()
    marker_path = root / GENERATION_MARKER_FILENAME
    if not marker_path.is_file():
        raise ValidationError(
            f"data generation marker is missing: {GENERATION_MARKER_FILENAME}"
        )
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"data generation marker cannot be parsed: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("data generation marker must be a JSON object")
    gen_id = data.get("generation_id")
    if not isinstance(gen_id, str) or not gen_id.strip():
        raise ValidationError("data generation marker is missing a valid generation_id")
    st = os.stat(root)
    return {
        "generation_id": gen_id,
        "data_dir": str(root),
        "st_dev": st.st_dev,
        "st_ino": st.st_ino,
    }


def open_sqlite_identity(conn: sqlite3.Connection) -> dict:
    """Inspect an actually open SQLite connection's file path, device/inode, head, and mode."""
    db_rows = conn.execute("PRAGMA database_list").fetchall()
    main_file = next((row[2] for row in db_rows if row[1] == "main"), "")
    if not main_file:
        raise ValidationError("open SQLite connection has no backing file")
    resolved = Path(main_file).resolve()
    st = os.stat(resolved)
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    return {
        "path": str(resolved),
        "st_dev": st.st_dev,
        "st_ino": st.st_ino,
        "user_version": user_version,
        "journal_mode": journal_mode,
    }


def _path_identity(path: Path) -> dict:
    resolved = path.resolve()
    st = os.stat(resolved)
    return {"path": str(resolved), "st_dev": st.st_dev, "st_ino": st.st_ino}


def resolve_release_pool_path(
    release_root: str | Path,
    pool_path: str | Path | None = None,
) -> Path:
    """Resolve the exam pool file inside `release_root` without hard-coding a pool filename."""
    if pool_path is not None:
        return Path(pool_path).resolve()
    root = Path(release_root).resolve()
    current_root = Path(sigint101.__file__).resolve().parent.parent
    if root == current_root and POOL_PATH.is_file():
        return POOL_PATH.resolve()
    exam_dir = root / "sigint101" / "exam_data"
    if exam_dir.is_dir():
        candidates = sorted(
            p for p in exam_dir.glob("*.json") if p.is_file() and not p.is_symlink()
        )
        if len(candidates) == 1:
            return candidates[0].resolve()
        if len(candidates) > 1:
            for cand in candidates:
                if cand.name == POOL_PATH.name:
                    return cand.resolve()
            raise ValidationError(
                f"ambiguous exam pool files in {exam_dir.name}: "
                + ", ".join(c.name for c in candidates)
            )
    return (exam_dir / POOL_PATH.name).resolve()


def check_saved_state_table_coverage(
    *,
    progress_tables: set[str] | frozenset[str] | None = None,
    exam_tables: set[str] | frozenset[str] | None = None,
) -> None:
    """Fail if any reviewed v1 table is neither checked nor explicitly on a no-references list."""
    if CHECKED_PROGRESS_TABLES & NO_REFERENCE_PROGRESS_TABLES:
        overlap = sorted(CHECKED_PROGRESS_TABLES & NO_REFERENCE_PROGRESS_TABLES)
        raise ValidationError(f"progress tables listed as both checked and no-ref: {overlap}")
    if CHECKED_EXAM_TABLES & NO_REFERENCE_EXAM_TABLES:
        overlap = sorted(CHECKED_EXAM_TABLES & NO_REFERENCE_EXAM_TABLES)
        raise ValidationError(f"exam tables listed as both checked and no-ref: {overlap}")

    prog_v1 = (
        set(reviewed_signatures("progress")["v1"]["tables"].keys())
        if progress_tables is None
        else set(progress_tables)
    )
    exam_v1 = (
        set(reviewed_signatures("exam")["v1"]["tables"].keys())
        if exam_tables is None
        else set(exam_tables)
    )

    uncovered_prog = sorted(prog_v1 - (CHECKED_PROGRESS_TABLES | NO_REFERENCE_PROGRESS_TABLES))
    if uncovered_prog:
        raise ValidationError(
            "progress.db v1 tables missing from saved-state validator: "
            + ", ".join(uncovered_prog)
        )
    uncovered_exam = sorted(exam_v1 - (CHECKED_EXAM_TABLES | NO_REFERENCE_EXAM_TABLES))
    if uncovered_exam:
        raise ValidationError(
            "exam.db v1 tables missing from saved-state validator: " + ", ".join(uncovered_exam)
        )


def attest_open_pair(
    *,
    progress: Progress,
    exam_store: ExamStore,
    data_dir: str | Path,
    pack_dir: str | Path | None = None,
    pool_path: str | Path | None = None,
    package_root: str | Path | None = None,
    require_release_manifest: bool = True,
    settings_fingerprint: str | None = None,
) -> dict:
    """Build a live attestation from actual imported package, pack, pool, and open DB handles."""
    actual_pkg_root = (
        Path(package_root).resolve()
        if package_root is not None
        else Path(sigint101.__file__).resolve().parent.parent
    )
    if require_release_manifest:
        release_info = read_release_manifest(actual_pkg_root)
    else:
        st = os.stat(actual_pkg_root)
        release_info = {
            "release_id": None,
            "commit": None,
            "package_root": str(actual_pkg_root),
            "st_dev": st.st_dev,
            "st_ino": st.st_ino,
        }

    resolved_pack = (
        Path(pack_dir).resolve()
        if pack_dir is not None
        else (actual_pkg_root / "lesson_packs").resolve()
    )
    resolved_pool = resolve_release_pool_path(actual_pkg_root, pool_path=pool_path)
    if require_release_manifest and not resolved_pack.is_relative_to(actual_pkg_root):
        raise ValidationError("loaded lesson pack directory lies outside the release root")
    if require_release_manifest and not resolved_pool.is_relative_to(actual_pkg_root):
        raise ValidationError("loaded exam pool lies outside the release root")
    pack_info = _path_identity(resolved_pack) if resolved_pack.is_dir() else None
    pool_info = _path_identity(resolved_pool) if resolved_pool.is_file() else None

    gen_info = read_generation_marker(data_dir)
    prog_id = open_sqlite_identity(progress._conn)
    exam_id = open_sqlite_identity(exam_store._conn)

    resolved_data = Path(data_dir).resolve()
    for name, db_id, filename in (
        ("progress", prog_id, "progress.db"),
        ("exam", exam_id, "exam.db"),
    ):
        expected_file = (resolved_data / filename).resolve()
        if not expected_file.is_file():
            raise ValidationError(f"expected database file missing: {filename}")
        exp_st = os.stat(expected_file)
        if (db_id["st_dev"], db_id["st_ino"]) != (exp_st.st_dev, exp_st.st_ino):
            raise ValidationError(
                f"open {name} database inode does not match data generation {resolved_data.name}"
            )

    return {
        "release": release_info,
        "pack": pack_info,
        "pool": pool_info,
        "generation": gen_info,
        "databases": {
            "progress": prog_id,
            "exam": exam_id,
        },
        "settings_fingerprint": settings_fingerprint,
    }


def verify_pair_attestation(
    attestation: dict,
    *,
    expected_release_id: str | None = None,
    expected_commit: str | None = None,
    expected_package_root: str | Path | None = None,
    expected_pack_dir: str | Path | None = None,
    expected_pool_path: str | Path | None = None,
    expected_generation_id: str | None = None,
    expected_data_dir: str | Path | None = None,
) -> None:
    """Verify a trial attestation against the exact expected release root, pack, pool, and DBs."""
    if not isinstance(attestation, dict):
        raise ValidationError("missing pair attestation payload")

    rel = attestation.get("release") or {}
    gen = attestation.get("generation") or {}
    dbs = attestation.get("databases") or {}

    if expected_release_id is not None and rel.get("release_id") != expected_release_id:
        raise ValidationError(
            f"release_id mismatch: attested {rel.get('release_id')!r}, "
            f"expected {expected_release_id!r}"
        )
    if expected_commit is not None and rel.get("commit") != expected_commit:
        raise ValidationError(
            f"commit mismatch: attested {rel.get('commit')!r}, expected {expected_commit!r}"
        )
    if expected_package_root is not None:
        exp_root = Path(expected_package_root).resolve()
        exp_st = os.stat(exp_root)
        if (
            rel.get("package_root") != str(exp_root)
            or rel.get("st_dev") != exp_st.st_dev
            or rel.get("st_ino") != exp_st.st_ino
        ):
            raise ValidationError("package root identity mismatch in attestation")

        exp_pack = (
            Path(expected_pack_dir).resolve()
            if expected_pack_dir is not None
            else (exp_root / "lesson_packs").resolve()
        )
        exp_pack_st = os.stat(exp_pack)
        pack_att = attestation.get("pack") or {}
        if (
            pack_att.get("path") != str(exp_pack)
            or pack_att.get("st_dev") != exp_pack_st.st_dev
            or pack_att.get("st_ino") != exp_pack_st.st_ino
        ):
            raise ValidationError("lesson pack identity mismatch in attestation")

        exp_pool = resolve_release_pool_path(exp_root, pool_path=expected_pool_path)
        exp_pool_st = os.stat(exp_pool)
        pool_att = attestation.get("pool") or {}
        if (
            pool_att.get("path") != str(exp_pool)
            or pool_att.get("st_dev") != exp_pool_st.st_dev
            or pool_att.get("st_ino") != exp_pool_st.st_ino
        ):
            raise ValidationError("exam pool identity mismatch in attestation")

    if expected_generation_id is not None and gen.get("generation_id") != expected_generation_id:
        raise ValidationError(
            f"generation_id mismatch: attested {gen.get('generation_id')!r}, "
            f"expected {expected_generation_id!r}"
        )

    prog_att = dbs.get("progress") or {}
    exam_att = dbs.get("exam") or {}
    if prog_att.get("user_version") != PROGRESS_SCHEMA_VERSION:
        raise ValidationError(
            f"progress schema head mismatch: {prog_att.get('user_version')} != "
            f"{PROGRESS_SCHEMA_VERSION}"
        )
    if exam_att.get("user_version") != EXAM_SCHEMA_VERSION:
        raise ValidationError(
            f"exam schema head mismatch: {exam_att.get('user_version')} != {EXAM_SCHEMA_VERSION}"
        )
    if prog_att.get("journal_mode") != "delete" or exam_att.get("journal_mode") != "delete":
        raise ValidationError("attested databases must be in journal_mode=delete")

    if expected_data_dir is not None:
        exp_data = Path(expected_data_dir).resolve()
        exp_data_st = os.stat(exp_data)
        if (
            gen.get("data_dir") != str(exp_data)
            or gen.get("st_dev") != exp_data_st.st_dev
            or gen.get("st_ino") != exp_data_st.st_ino
        ):
            raise ValidationError("data generation directory identity mismatch in attestation")
        for key, filename in (("progress", "progress.db"), ("exam", "exam.db")):
            actual = dbs.get(key) or {}
            exp_file = (exp_data / filename).resolve()
            exp_st = os.stat(exp_file)
            if (
                actual.get("path") != str(exp_file)
                or actual.get("st_dev") != exp_st.st_dev
                or actual.get("st_ino") != exp_st.st_ino
            ):
                raise ValidationError(f"open {key} database inode/path mismatch in attestation")


def validate_course_content(
    *,
    lessons: dict[int, Lesson],
    exam_pool: Pool | None,
    exam_disabled_reason: str | None,
    templates_dir: str | Path,
    required_lesson_ids: frozenset[int] | set[int] | None = None,
) -> list[str]:
    """Validate course packs, templates, and verified exam pool; return all error reasons."""
    errors: list[str] = []
    if not lessons:
        errors.append("lesson pack directory is empty")
    else:
        required = (
            SHIPPED_REQUIRED_LESSONS
            if required_lesson_ids is None
            else frozenset(required_lesson_ids)
        )
        missing = sorted(required - set(lessons.keys()))
        if missing:
            errors.append(
                "missing required lesson packs: " + ", ".join(f"lesson {i}" for i in missing)
            )
        for lid in sorted(lessons):
            if unit_for_lesson(lid) is None:
                errors.append(f"lesson {lid} is not placed in any curriculum unit")

    tpl_path = Path(templates_dir)
    if not tpl_path.is_dir():
        errors.append("templates directory is missing")
    else:
        env = Environment(loader=FileSystemLoader(str(tpl_path)))
        for name in REQUIRED_TEMPLATES:
            if not (tpl_path / name).is_file():
                errors.append(f"missing required template: {name}")
                continue
            try:
                env.get_template(name)
            except TemplateSyntaxError as exc:
                errors.append(f"broken template {name}: {exc}")

    if exam_pool is None or exam_disabled_reason is not None:
        detail = exam_disabled_reason or "pool not loaded"
        errors.append(f"exam pool unavailable: {detail}")
    else:
        try:
            exam_pool.verify()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"exam pool failed verification: {exc}")

    return errors


def _default_challenge_for(unit_id: int):
    """Look up a unit's live-find challenge if sigint101.challenges is present in this release."""
    try:
        from . import challenges as _challenges_mod  # noqa: PLC0415
    except ImportError:
        return None
    return _challenges_mod.challenge_for(unit_id)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()}


def validate_saved_state(
    *,
    lessons: dict[int, Lesson],
    exam_pool: Pool | None,
    progress: Progress,
    exam_store: ExamStore,
    challenge_for_fn: Callable[[int], object] | None = None,
) -> list[str]:
    """Verify every persisted reference in progress.db and exam.db against the loaded course."""
    errors: list[str] = []
    known_instruments = {inst.key for inst in INSTRUMENTS}
    stamp_instruments = frozenset(STAMP_INSTRUMENTS)
    lookup_challenge = challenge_for_fn or _default_challenge_for

    with progress._lock:
        p_conn = progress._conn
        existing_prog_tables = {
            row[0]
            for row in p_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        for row in p_conn.execute("SELECT lesson_id FROM progress ORDER BY lesson_id"):
            lid = row["lesson_id"]
            if lid not in lessons:
                errors.append(f"progress references unknown lesson_id {lid}")

        for row in p_conn.execute(
            "SELECT lesson_id, stage, furthest FROM stages ORDER BY lesson_id"
        ):
            lid, stage, furthest = row["lesson_id"], row["stage"], row["furthest"]
            lesson = lessons.get(lid)
            if lesson is None:
                errors.append(f"stages references unknown lesson_id {lid}")
                continue
            order = lesson.stage_order
            if stage not in order:
                errors.append(f"stages for lesson {lid} references unknown stage {stage!r}")
            if not isinstance(furthest, int) or furthest < 0 or furthest >= len(order):
                errors.append(
                    f"stages for lesson {lid} has out-of-range furthest index {furthest!r}"
                )

        for row in p_conn.execute(
            "SELECT lesson_id, kind, body FROM responses ORDER BY lesson_id, kind"
        ):
            lid, kind, body = row["lesson_id"], row["kind"], row["body"]
            lesson = lessons.get(lid)
            if lesson is None:
                errors.append(f"responses ({kind}) references unknown lesson_id {lid}")
                continue
            if kind == PREDICTION:
                if not str(body or "").strip():
                    errors.append(f"responses prediction for lesson {lid} is blank")
                elif lesson.choices:
                    valid_keys = {c["key"] for c in lesson.choices}
                    if body not in valid_keys:
                        errors.append(
                            f"responses prediction for lesson {lid} has unknown choice {body!r}"
                        )
            elif kind == CHECK:
                if not lesson.check or CHECK_STAGE not in lesson.stage_order:
                    errors.append(f"responses check for lesson {lid}, but lesson has no check")
                else:
                    valid_check = {c["key"] for c in lesson.check["choices"]}
                    if body not in valid_check:
                        errors.append(
                            f"responses check for lesson {lid} has unknown choice {body!r}"
                        )
            else:
                errors.append(f"responses for lesson {lid} has unknown kind {kind!r}")

        for row in p_conn.execute(
            "SELECT lesson_id, card_key, choice FROM sort_answers ORDER BY lesson_id, card_key"
        ):
            lid, card_key, choice = row["lesson_id"], row["card_key"], row["choice"]
            lesson = lessons.get(lid)
            if lesson is None:
                errors.append(f"sort_answers references unknown lesson_id {lid}")
                continue
            if not lesson.sort:
                errors.append(f"sort_answers for lesson {lid}, but lesson has no sort deck")
                continue
            valid_cards = {card["key"] for card in lesson.sort}
            if card_key not in valid_cards:
                errors.append(
                    f"sort_answers for lesson {lid} references unknown card_key {card_key!r}"
                )
            if choice not in SORT_ANSWERS:
                errors.append(
                    f"sort_answers for lesson {lid} card {card_key!r} has invalid choice {choice!r}"
                )

        for row in p_conn.execute("SELECT name, value FROM places ORDER BY name"):
            name, value = row["name"], row["value"]
            if name == "rack" and value not in known_instruments:
                errors.append(f"places rack references unknown instrument {value!r}")

        if "stamps" in existing_prog_tables:
            for row in p_conn.execute("SELECT instrument FROM stamps ORDER BY instrument"):
                inst_key = row["instrument"]
                if inst_key not in stamp_instruments:
                    errors.append(f"stamps references unknown instrument {inst_key!r}")

        if "retrieval" in existing_prog_tables:
            for row in p_conn.execute(
                "SELECT lesson_id, box, last_choice FROM retrieval ORDER BY lesson_id"
            ):
                lid, box, last_choice = row["lesson_id"], row["box"], row["last_choice"]
                lesson = lessons.get(lid)
                if lesson is None:
                    errors.append(f"retrieval references unknown lesson_id {lid}")
                    continue
                if not lesson.check:
                    errors.append(f"retrieval for lesson {lid}, but lesson has no check")
                if (
                    not isinstance(box, int)
                    or isinstance(box, bool)
                    or box < 1
                    or box > RETRIEVAL_MAX_BOX
                ):
                    errors.append(f"retrieval for lesson {lid} has out-of-range box {box!r}")
                if last_choice is not None:
                    if not isinstance(last_choice, str) or not last_choice.strip():
                        errors.append(
                            f"retrieval for lesson {lid} has blank last_choice {last_choice!r}"
                        )
                    elif not lesson.check:
                        errors.append(
                            f"retrieval for lesson {lid} has last_choice {last_choice!r} "
                            "with no check"
                        )
                    else:
                        valid_keys = {c["key"] for c in lesson.check["choices"]}
                        if last_choice not in valid_keys:
                            errors.append(
                                f"retrieval for lesson {lid} has unknown last_choice "
                                f"{last_choice!r}"
                            )

        if "challenges" in existing_prog_tables:
            cols = _table_columns(p_conn, "challenges")
            select_cols = ["unit_id", "window_s"]
            if "instrument" in cols:
                select_cols.append("instrument")
            for row in p_conn.execute(
                f"SELECT {', '.join(select_cols)} FROM challenges ORDER BY unit_id"
            ):
                uid = row["unit_id"]
                window_s = row["window_s"]
                if lookup_challenge(uid) is None:
                    errors.append(f"challenges references unit {uid} with no challenge")
                if (
                    not isinstance(window_s, (int, float))
                    or isinstance(window_s, bool)
                    or window_s <= 0
                ):
                    errors.append(f"challenges for unit {uid} has invalid window_s {window_s!r}")
                if "instrument" in cols and row["instrument"] not in (None, ""):
                    if row["instrument"] not in stamp_instruments:
                        errors.append(
                            f"challenges for unit {uid} references unknown instrument "
                            f"{row['instrument']!r}"
                        )

        if "challenge_passes" in existing_prog_tables:
            cols = _table_columns(p_conn, "challenge_passes")
            select_cols = ["unit_id", "instrument"]
            if "window_s" in cols:
                select_cols.append("window_s")
            for row in p_conn.execute(
                f"SELECT {', '.join(select_cols)} FROM challenge_passes ORDER BY unit_id"
            ):
                uid = row["unit_id"]
                inst_key = row["instrument"]
                if lookup_challenge(uid) is None:
                    errors.append(f"challenge_passes references unit {uid} with no challenge")
                if inst_key not in stamp_instruments:
                    errors.append(
                        f"challenge_passes for unit {uid} references unknown instrument "
                        f"{inst_key!r}"
                    )
                if "window_s" in cols:
                    window_s = row["window_s"]
                    if (
                        not isinstance(window_s, (int, float))
                        or isinstance(window_s, bool)
                        or window_s <= 0
                    ):
                        errors.append(
                            f"challenge_passes for unit {uid} has invalid window_s {window_s!r}"
                        )

        if "replays_seen" in existing_prog_tables:
            for row in p_conn.execute(
                "SELECT instrument, count, recording, digest FROM replays_seen ORDER BY instrument"
            ):
                inst_key = row["instrument"]
                count = row["count"]
                recording = row["recording"]
                digest = row["digest"]
                if inst_key not in known_instruments:
                    errors.append(f"replays_seen references unknown instrument {inst_key!r}")
                if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                    errors.append(f"replays_seen for {inst_key!r} has invalid count {count!r}")
                if not isinstance(recording, str) or not recording.strip():
                    errors.append(f"replays_seen for {inst_key!r} has blank recording")
                if not isinstance(digest, str) or not digest.strip():
                    errors.append(f"replays_seen for {inst_key!r} has blank digest")

    with exam_store._lock:
        e_conn = exam_store._conn
        for row in e_conn.execute("SELECT question_id, box FROM srs ORDER BY question_id"):
            qid, box = row["question_id"], row["box"]
            if exam_pool is not None and exam_pool.get(qid) is None:
                errors.append(f"exam srs references unknown question_id {qid!r}")
            if not isinstance(box, int) or box < 1 or box > LEITNER_BOXES:
                errors.append(f"exam srs for {qid!r} has invalid box {box!r}")

        for row in e_conn.execute("SELECT id, question_ids, answers, skipped FROM sim ORDER BY id"):
            try:
                qids = json.loads(row["question_ids"])
                answers = json.loads(row["answers"])
                skipped = json.loads(row["skipped"])
            except (ValueError, TypeError) as exc:
                errors.append(f"exam sim row {row['id']} contains invalid JSON: {exc}")
                continue
            if not isinstance(qids, list) or not all(isinstance(q, str) and q for q in qids):
                errors.append(f"exam sim row {row['id']} has malformed question_ids")
                continue
            if not isinstance(answers, dict):
                errors.append(f"exam sim row {row['id']} has malformed answers")
                continue
            if not isinstance(skipped, list) or not all(isinstance(q, str) for q in skipped):
                errors.append(f"exam sim row {row['id']} has malformed skipped list")
                continue

            qid_set = set(qids)
            for qid in qids:
                if exam_pool is not None and exam_pool.get(qid) is None:
                    errors.append(f"exam sim question_ids references unknown question {qid!r}")
            for qid, choice in answers.items():
                if qid not in qid_set:
                    errors.append(f"exam sim answer references off-sheet question {qid!r}")
                    continue
                if exam_pool is not None:
                    q = exam_pool.get(qid)
                    if q is None:
                        errors.append(f"exam sim answer references unknown question {qid!r}")
                    elif choice not in q.options:
                        errors.append(
                            f"exam sim answer for {qid!r} has invalid option {choice!r}"
                        )
            for qid in skipped:
                if qid not in qid_set:
                    errors.append(f"exam sim skipped references off-sheet question {qid!r}")
                elif exam_pool is not None and exam_pool.get(qid) is None:
                    errors.append(f"exam sim skipped references unknown question {qid!r}")

    return errors
