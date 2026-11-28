# ABOUTME: SQLite progress store — lessons opened and completed, and what the learner predicted.
# ABOUTME: Stdlib sqlite3, thread-safe for the kiosk (Starlette runs sync routes off-thread).

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

from . import retrieval
from .challenges import CHALLENGE_KIND, AttemptKind

# The store lives under data/ by default, which .gitignore keeps out of the repo so a
# learner's progress is never committed. Tests pass an explicit temp path instead, and so
# never touch this shared file.
DEFAULT_DB_PATH = "data/progress.db"

# CREATE IF NOT EXISTS on every open, instead of a migration step: the kiosk has no
# deploy pipeline, so the database a power cut left behind must come up as-is and gain
# any new table the moment the console starts.
SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
    lesson_id INTEGER PRIMARY KEY,
    opened    INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    updated   REAL
);
"""

# Free-text answers, one row per lesson per kind. A separate table rather than
# columns on progress, so an earlier build's database gains it on open instead of
# needing a migration.
SCHEMA += """
CREATE TABLE IF NOT EXISTS responses (
    lesson_id INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    body      TEXT NOT NULL,
    updated   REAL,
    PRIMARY KEY (lesson_id, kind)
);
"""

# Where the learner is inside each lesson: the stage last shown, and the furthest position (index
# in the lesson's stage order) ever reached, so a reload or a power cut resumes in place and the
# rail keeps every visited stage reachable. Its own table for the same reason as responses: a
# database from an earlier build gains it on open, with its progress and predictions untouched.
SCHEMA += """
CREATE TABLE IF NOT EXISTS stages (
    lesson_id INTEGER PRIMARY KEY,
    stage     TEXT NOT NULL,
    furthest  INTEGER NOT NULL DEFAULT 0,
    updated   REAL
);
"""

# Where the learner was left outside a lesson, one row per place: the rack's last instrument, so
# /operate reopens on it after a reload or a power cut the way a lesson resumes its stage. Its own
# table, so an earlier build's database gains it on open.
SCHEMA += """
CREATE TABLE IF NOT EXISTS places (
    name    TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated REAL
);
"""

# A badge keeps its first evidence even after the source line rotates away. The event digests
# track which lines of the retained log have already contributed to each count.
SCHEMA += """
CREATE TABLE IF NOT EXISTS stamps (
    instrument TEXT PRIMARY KEY,
    first_ts   REAL NOT NULL,
    count      INTEGER NOT NULL,
    provenance TEXT NOT NULL,
    digest     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stamp_events (
    digest BLOB PRIMARY KEY
) WITHOUT ROWID;
"""

# Card-sort answers (M5's 12-card law sort): additive table so existing databases gain it on open.
# Score is shown to the learner for feedback and never gates completion (D16).
SCHEMA += """
CREATE TABLE IF NOT EXISTS sort_answers (
    lesson_id INTEGER NOT NULL,
    card_key  TEXT NOT NULL,
    choice    TEXT NOT NULL,
    correct   INTEGER NOT NULL,
    updated   REAL,
    PRIMARY KEY (lesson_id, card_key)
);
"""

# Pre-lesson retrieval card schedule: additive table so existing databases gain it on open.
# Kept apart from responses so answering a recall card never touches a lesson's own check.
SCHEMA += """
CREATE TABLE IF NOT EXISTS retrieval (
    lesson_id   INTEGER PRIMARY KEY,
    box         INTEGER NOT NULL DEFAULT 1,
    last_seen   REAL NOT NULL,
    seen        INTEGER NOT NULL DEFAULT 0,
    correct     INTEGER NOT NULL DEFAULT 0,
    last_choice TEXT
);
"""

# Tier B live-find attempts and permanent pass history: additive tables so existing databases
# gain them on open without touching any existing table or gating any lesson (D16).
SCHEMA += """
CREATE TABLE IF NOT EXISTS challenges (
    unit_id    INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,
    window_s   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS challenge_passes (
    unit_id    INTEGER NOT NULL,
    started_at REAL NOT NULL,
    passed_at  REAL NOT NULL,
    instrument TEXT NOT NULL,
    provenance TEXT NOT NULL,
    digest     TEXT NOT NULL,
    PRIMARY KEY (unit_id, started_at)
);
"""

# The separate replay ledger records first evidence and a count without changing live stamps.
SCHEMA += """
CREATE TABLE IF NOT EXISTS replays_seen (
    instrument TEXT PRIMARY KEY,
    first_ts   REAL NOT NULL,
    count      INTEGER NOT NULL,
    recording  TEXT NOT NULL,
    digest     TEXT NOT NULL
);
"""

# Tier C per-unit solos and capstone attempts, per-leg reception passes, and legal calls:
# additive tables so existing databases gain them on open without touching any existing table.
SCHEMA += """
CREATE TABLE IF NOT EXISTS solo_attempts (
    solo_id    TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    window_s   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS solo_passes (
    solo_id    TEXT NOT NULL,
    started_at REAL NOT NULL,
    band_slot  INTEGER NOT NULL,
    passed_at  REAL NOT NULL,
    instrument TEXT NOT NULL,
    provenance TEXT NOT NULL,
    digest     TEXT NOT NULL,
    PRIMARY KEY (solo_id, started_at, band_slot)
);
CREATE TABLE IF NOT EXISTS solo_calls (
    solo_id       TEXT NOT NULL,
    started_at    REAL NOT NULL,
    band_slot     INTEGER NOT NULL,
    call          TEXT NOT NULL,
    why_key       TEXT NOT NULL,
    call_correct  INTEGER NOT NULL,
    why_correct   INTEGER NOT NULL,
    answered_at   REAL NOT NULL,
    item_revision TEXT NOT NULL,
    PRIMARY KEY (solo_id, started_at, band_slot)
);
"""

# The `kind` key for the learner's pre-observation guess. A constant because the predict
# gate and the lesson view must agree on the exact string, or the gate would never unlock.
PREDICTION = "prediction"

# The `kind` key for the learner's answer to a lesson's post-explain check: the chip key chosen.
CHECK = "check"


class PredictionAlreadyCommitted(RuntimeError):
    """A second commit for a lesson whose prediction is already on record.

    A prediction is the learner's word before the reveal. Once the explain has shown the answer,
    accepting a new one would let the score copy the reveal, so the store refuses it outright.
    """


class CheckAlreadyAnswered(RuntimeError):
    """A second answer to a lesson's check.

    The feedback after the first answer names the key, so a re-answer would only copy it.
    """


class RecallNotDue(RuntimeError):
    """A recall answer for a card that is not enrolled or not yet due.

    Refused under the store lock so a resubmitted form cannot climb a box twice.
    """


def _add_correct_column(conn) -> None:
    """Give an earlier build's responses table the `correct` column, keeping every row it holds.

    CREATE IF NOT EXISTS cannot add a column to a table that already exists, so a database from
    before scoring is checked on open and altered in place; a fresh one is left as SCHEMA made it.
    NULL means "not scored" (the pack had no answer key), never a guessed wrong.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(responses)")}
    if "correct" not in columns:
        conn.execute("ALTER TABLE responses ADD COLUMN correct INTEGER")


class Progress:
    """Where the learner's journey through a lesson is kept.

    SQLite rather than a file because the console is a kiosk that loses power in the
    field: a half-written row must not cost the learner their progress.
    """

    def __init__(self, db_path=DEFAULT_DB_PATH, *, managed: bool = False, read_only: bool = False):
        self.db_path = str(db_path)
        # check_same_thread=False + a lock: the FastAPI threadpool touches this from
        # a different thread than the one that opened it. The lock serializes access.
        self._lock = threading.Lock()
        self._closed = False
        self._quarantined_reason: str | None = None
        if managed:
            from .migrations import PROGRESS_SCHEMA_VERSION, reviewed_signatures
            from .storage import open_managed_sqlite

            self._conn = open_managed_sqlite(
                self.db_path,
                expected_version=PROGRESS_SCHEMA_VERSION,
                expected_signature=reviewed_signatures("progress")["v1"],
                read_only=read_only,
            )
            return
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(SCHEMA)
            _add_correct_column(self._conn)
            self._conn.commit()

    def mark_opened(self, lesson_id: int, *, clock=None) -> None:
        """Note that the learner has looked at a lesson, without claiming they did it."""
        updated = clock() if clock else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO progress(lesson_id, opened, updated) VALUES(?, 1, ?) "
                "ON CONFLICT(lesson_id) DO UPDATE SET opened=1, updated=excluded.updated",
                (lesson_id, updated),
            )
            self._conn.commit()

    def mark_completed(self, lesson_id: int, *, clock=None) -> None:
        """Note that the learner finished a lesson.

        Also sets opened, so a lesson can never be recorded as completed but unopened
        however the rows were written.
        """
        updated = clock() if clock else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO progress(lesson_id, opened, completed, updated) VALUES(?, 1, 1, ?) "
                "ON CONFLICT(lesson_id) DO UPDATE SET "
                "completed=1, opened=1, updated=excluded.updated",
                (lesson_id, updated),
            )
            self._conn.commit()

    def record_prediction(self, lesson_id: int, body: str, *, correct=None, clock=None) -> None:
        """Store what the learner expects to see, whether it was right, and open the lesson.

        Fails closed on a blank answer: an empty prediction is not a prediction, and
        accepting one would unlock the explanation without the learner committing first.
        `correct` is the lesson's own scoring of the chip (None when the pack names no answer).
        A lesson that already has a prediction refuses a second one (PredictionAlreadyCommitted).
        """
        body = (body or "").strip()
        if not body:
            raise ValueError("a prediction cannot be blank")

        updated = clock() if clock else None
        with self._lock:
            # Both writes share one lock and one commit: a prediction that saved without
            # opening the lesson, or the reverse, would leave the console showing a state
            # the learner never reached. DO NOTHING on a collision, then a zero rowcount, is
            # how a second commit is caught: the first prediction stands untouched.
            inserted = self._insert_response(lesson_id, PREDICTION, body, updated, correct)
            if not inserted:
                self._conn.rollback()
                raise PredictionAlreadyCommitted(
                    f"lesson {lesson_id} already has a committed prediction")
            self._conn.execute(
                "INSERT INTO progress(lesson_id, opened, updated) VALUES(?, 1, ?) "
                "ON CONFLICT(lesson_id) DO UPDATE SET opened=1, updated=excluded.updated",
                (lesson_id, updated),
            )
            self._conn.commit()

    def _insert_response(self, lesson_id: int, kind: str, body: str, updated, correct) -> bool:
        """Write one response row unless one of this kind is already there; True when written.

        The caller holds the lock and commits. `correct` is stored as 1/0, or NULL when unscored.
        """
        stored_correct = None if correct is None else int(bool(correct))
        return bool(self._conn.execute(
            "INSERT INTO responses(lesson_id, kind, body, updated, correct) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(lesson_id, kind) DO NOTHING",
            (lesson_id, kind, body, updated, stored_correct),
        ).rowcount)

    def record_check(self, lesson_id: int, body: str, *, correct: bool, clock=None) -> None:
        """Store the chip the learner chose on a lesson's check, and whether it was the key.

        Fails closed on a blank answer, and refuses a second one (CheckAlreadyAnswered): the
        feedback after the first names the key, so a later answer would only copy it.
        """
        body = (body or "").strip()
        if not body:
            raise ValueError("a check answer cannot be blank")
        updated = clock() if clock else None
        with self._lock:
            if not self._insert_response(lesson_id, CHECK, body, updated, correct):
                self._conn.rollback()
                raise CheckAlreadyAnswered(f"lesson {lesson_id} already has a check answer")
            self._conn.commit()

    def get_check(self, lesson_id: int):
        """The check answer as {"choice", "correct"}, or None if the check is unanswered."""
        with self._lock:
            row = self._conn.execute(
                "SELECT body, correct FROM responses WHERE lesson_id=? AND kind=?",
                (lesson_id, CHECK),
            ).fetchone()
        if row is None:
            return None
        return {"choice": row["body"], "correct": bool(row["correct"])}

    def record_sort_answer(self, lesson_id: int, card_key: str, choice: str, *,
                           correct: bool, clock=None) -> None:
        """Store one card-sort answer, updating in place if re-sorted."""
        card_key = (card_key or "").strip()
        choice = (choice or "").strip()
        if not card_key:
            raise ValueError("a sort card key cannot be blank")
        if not choice:
            raise ValueError("a sort choice cannot be blank")
        updated = clock() if clock else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO sort_answers(lesson_id, card_key, choice, correct, updated) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(lesson_id, card_key) DO UPDATE SET "
                "choice=excluded.choice, correct=excluded.correct, updated=excluded.updated",
                (lesson_id, card_key, choice, int(bool(correct)), updated),
            )
            self._conn.commit()

    def get_sort_answers(self, lesson_id: int) -> dict[str, dict]:
        """All recorded card-sort answers for a lesson, keyed by card_key."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT card_key, choice, correct FROM sort_answers WHERE lesson_id=?",
                (lesson_id,),
            ).fetchall()
        return {
            row["card_key"]: {"choice": row["choice"], "correct": bool(row["correct"])}
            for row in rows
        }

    def get_prediction(self, lesson_id: int):
        """What the learner said they expected to see, or None if they have not said.

        None is what seals the lesson: everything downstream stays hidden until there
        is an answer here.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM responses WHERE lesson_id=? AND kind=?",
                (lesson_id, PREDICTION),
            ).fetchone()
        return row["body"] if row else None

    def get_correct(self, lesson_id: int):
        """Whether the committed prediction was right: True, False, or None (unscored/absent)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT correct FROM responses WHERE lesson_id=? AND kind=?",
                (lesson_id, PREDICTION),
            ).fetchone()
        if row is None or row["correct"] is None:
            return None
        return bool(row["correct"])

    def record_stage(self, lesson_id: int, stage: str, position: int, *, clock=None) -> None:
        """Remember the stage the learner is on, and keep the furthest position ever reached.

        `position` is the stage's index in the lesson's own order. Stepping back lowers the
        current stage but never the furthest, so going back to re-read the setup does not lock
        the stages already seen. Fails closed on a blank stage or a negative position, which no
        real walk produces.
        """
        stage = (stage or "").strip()
        if not stage:
            raise ValueError("a stage cannot be blank")
        if position < 0:
            raise ValueError("a stage position cannot be negative")

        updated = clock() if clock else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO stages(lesson_id, stage, furthest, updated) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(lesson_id) DO UPDATE SET stage=excluded.stage, "
                "furthest=MAX(furthest, excluded.furthest), updated=excluded.updated",
                (lesson_id, stage, position, updated),
            )
            self._conn.commit()

    def get_stage(self, lesson_id: int):
        """The stage last shown and the furthest position reached, or None if never recorded."""
        with self._lock:
            row = self._conn.execute(
                "SELECT stage, furthest FROM stages WHERE lesson_id=?", (lesson_id,)
            ).fetchone()
        return dict(row) if row else None

    def record_place(self, name: str, value: str, *, clock=None) -> None:
        """Remember where the learner was left in a place outside the lessons (the rack).

        Fails closed on a blank value, which no real visit produces.
        """
        value = (value or "").strip()
        if not value:
            raise ValueError("a place cannot be blank")
        updated = clock() if clock else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO places(name, value, updated) VALUES(?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (name, value, updated),
            )
            self._conn.commit()

    def get_place(self, name: str):
        """Where the learner was last left in a place, or None if never."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM places WHERE name=?", (name,)).fetchone()
        return row["value"] if row else None

    def get(self, lesson_id: int):
        """Everything known about one lesson's progress, prediction included."""
        with self._lock:
            # LEFT JOIN, not an inner one: a lesson that has been opened but not yet
            # predicted still has progress worth reporting, and must not vanish here.
            row = self._conn.execute(
                "SELECT p.lesson_id, p.opened, p.completed, p.updated, r.body AS prediction "
                "FROM progress p "
                "LEFT JOIN responses r ON r.lesson_id = p.lesson_id AND r.kind = ? "
                "WHERE p.lesson_id = ?",
                (PREDICTION, lesson_id),
            ).fetchone()
        return dict(row) if row else None

    def record_stamps(self, stamps, *, backfill=False) -> bool:
        """Count each event once, keeping the first evidence and any retired badge rows."""
        with self._lock:
            self._require_usable_unlocked()
            if self._closed:
                return False
            with self._conn:
                expired = ({row[0] for row in self._conn.execute("SELECT digest FROM stamp_events")}
                           if backfill else set())
                for stamp in stamps:
                    digest = bytes.fromhex(stamp.get("watermark", stamp["digest"]))
                    expired.discard(digest)
                    inserted = self._conn.execute(
                        "INSERT INTO stamp_events(digest) VALUES(?) ON CONFLICT DO NOTHING",
                        (digest,),
                    )
                    if inserted.rowcount:
                        self._conn.execute(
                            "INSERT INTO stamps(instrument, first_ts, count, provenance, digest) "
                            "VALUES(?, ?, 1, ?, ?) ON CONFLICT(instrument) DO UPDATE SET "
                            "count=stamps.count + 1",
                            (stamp["instrument"], stamp["first_ts"],
                             json.dumps(stamp["provenance"]), stamp["digest"]),
                        )
                if expired:
                    self._conn.executemany("DELETE FROM stamp_events WHERE digest=?",
                                           ((digest,) for digest in expired))
            return True

    def stamps(self) -> list[dict]:
        """The stamps earned so far, with the evidence of each first reception."""
        with self._lock:
            self._require_usable_unlocked()
            rows = self._conn.execute(
                "SELECT instrument, first_ts, count, provenance, digest FROM stamps"
            ).fetchall()
        return [{**dict(row), "provenance": json.loads(row["provenance"])} for row in rows]

    @staticmethod
    def _decode_pass_row(kind: AttemptKind, row) -> dict:
        out = dict(row)
        for col in kind.json_pass_columns:
            if col in out and isinstance(out[col], str):
                out[col] = json.loads(out[col])
        return out

    @staticmethod
    def _resolve_attempt_id(
        kind: AttemptKind,
        attempt_id: str | None,
        source: dict | None = None,
        *,
        generate: bool = False,
    ) -> str | None:
        if kind.identity_column is None:
            if attempt_id is not None:
                raise ValueError(
                    f"attempt kind {kind.kind!r} does not define an identity_column"
                )
            return None
        resolved = attempt_id
        if resolved is None and source is not None:
            resolved = source.get(kind.identity_column)
        if resolved is None and generate:
            resolved = uuid4().hex
        if not isinstance(resolved, str) or not resolved.strip():
            raise ValueError(
                f"attempt kind {kind.kind!r} requires a non-empty {kind.identity_column!r}"
            )
        return resolved

    @staticmethod
    def _pass_columns_and_values(
        kind: AttemptKind,
        key,
        started_at: float,
        slot,
        pass_row: dict,
        *,
        attempt_id: str | None = None,
    ) -> tuple[list[str], list, str | None]:
        if kind.slot_column is not None and slot is None:
            raise ValueError(f"attempt kind {kind.kind!r} requires a slot value")
        if kind.slot_column is None and slot is not None:
            raise ValueError(f"attempt kind {kind.kind!r} does not accept a slot value")
        resolved_id = Progress._resolve_attempt_id(kind, attempt_id, pass_row)
        missing = [col for col in kind.pass_columns if col not in pass_row]
        if missing:
            raise ValueError(
                f"attempt kind {kind.kind!r} pass_row missing columns: {', '.join(missing)}"
            )
        cols = [kind.key_column, "started_at"]
        vals = [key, started_at]
        if kind.identity_column is not None:
            cols.append(kind.identity_column)
            vals.append(resolved_id)
        if kind.slot_column is not None:
            cols.append(kind.slot_column)
            vals.append(slot)
        json_cols = set(kind.json_pass_columns)
        for col in kind.pass_columns:
            cols.append(col)
            val = pass_row[col]
            vals.append(json.dumps(val) if col in json_cols else val)
        return cols, vals, resolved_id

    @staticmethod
    def _prepare_guarded_pass_insert(
        kind: AttemptKind,
        key,
        started_at: float,
        slot,
        pass_row: dict,
        *,
        attempt_id: str | None = None,
    ) -> tuple[str, tuple]:
        """Build an atomic insert-once pass SQL guarded on the matching attempt row."""
        cols, vals, resolved_id = Progress._pass_columns_and_values(
            kind, key, started_at, slot, pass_row, attempt_id=attempt_id,
        )
        placeholders = ", ".join("?" for _ in cols)
        guards = [f"{kind.key_column}=?", "started_at=?"]
        guard_vals: list = [key, started_at]
        if kind.identity_column is not None:
            guards.append(f"{kind.identity_column}=?")
            guard_vals.append(resolved_id)
        for col in kind.replay_binding_columns:
            guards.append(f"{col}=?")
            guard_vals.append(pass_row[col])
        where_sql = " AND ".join(guards)
        sql = (
            f"INSERT INTO {kind.passes_table}({', '.join(cols)}) "
            f"SELECT {placeholders} "
            f"WHERE EXISTS ("
            f"  SELECT 1 FROM {kind.attempts_table} "
            f"  WHERE {where_sql}"
            f") ON CONFLICT DO NOTHING"
        )
        return sql, (*vals, *guard_vals)

    @staticmethod
    def _validate_attempt_extra(kind: AttemptKind, extra: dict) -> None:
        expected = set(kind.attempt_columns)
        provided = set(extra.keys())
        if provided != expected:
            missing = sorted(expected - provided)
            unexpected = sorted(provided - expected)
            raise ValueError(
                f"attempt kind {kind.kind!r} attempt columns mismatch "
                f"(missing={missing}, unexpected={unexpected})"
            )

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def quarantined_reason(self) -> str | None:
        with self._lock:
            return self._quarantined_reason

    def _require_usable_unlocked(self) -> None:
        if self._quarantined_reason is not None:
            raise sqlite3.OperationalError(self._quarantined_reason)

    def _quarantine_unlocked(self, reason: str) -> None:
        self._quarantined_reason = reason
        self._closed = True
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def _check_fresh_identity_unlocked(self, kind: AttemptKind, key, attempt_id: str) -> None:
        assert kind.identity_column is not None
        id_col = kind.identity_column
        existing = self._conn.execute(
            f"SELECT 1 FROM {kind.attempts_table} WHERE {kind.key_column}=? AND {id_col}=? "
            f"UNION ALL "
            f"SELECT 1 FROM {kind.passes_table} WHERE {kind.key_column}=? AND {id_col}=? "
            f"LIMIT 1",
            (key, attempt_id, key, attempt_id),
        ).fetchone()
        if existing is not None:
            raise ValueError(
                f"attempt kind {kind.kind!r} cannot reuse {id_col} {attempt_id!r}"
            )

    def _insert_attempt_pass_unlocked(
        self,
        kind: AttemptKind,
        key,
        started_at: float,
        slot,
        pass_row: dict,
        *,
        attempt_id: str | None = None,
    ) -> bool:
        sql, params = self._prepare_guarded_pass_insert(
            kind, key, started_at, slot, pass_row, attempt_id=attempt_id,
        )
        return bool(self._conn.execute(sql, params).rowcount)

    def stage_attempt(
        self,
        kind: AttemptKind,
        key,
        started_at: float,
        window_s: int,
        *,
        extra: dict | None = None,
        on_staged=None,
        attempt_id: str | None = None,
    ) -> bool:
        """Upsert an attempt in an uncommitted transaction; commit only if `on_staged` succeeds."""
        extra_cols = dict(extra or {})
        self._validate_attempt_extra(kind, extra_cols)
        resolved_id = self._resolve_attempt_id(kind, attempt_id, generate=True)
        cols = [kind.key_column, "started_at", "window_s"]
        update_cols = ["started_at", "window_s"]
        val_list: list = [key, started_at, window_s]
        if kind.identity_column is not None:
            cols.append(kind.identity_column)
            update_cols.append(kind.identity_column)
            val_list.append(resolved_id)
        cols.extend(kind.attempt_columns)
        update_cols.extend(kind.attempt_columns)
        val_list.extend(extra_cols[col] for col in kind.attempt_columns)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{col}=excluded.{col}" for col in update_cols)
        values = tuple(val_list)
        with self._lock:
            self._require_usable_unlocked()
            if self._closed:
                return False
            if self._conn.in_transaction:
                rb_err = None
                try:
                    self._conn.rollback()
                except sqlite3.Error as exc:
                    rb_err = exc
                if rb_err is not None or self._conn.in_transaction:
                    detail = str(rb_err) if rb_err is not None else "transaction remained open"
                    msg = f"progress store quarantined after unconfirmed rollback: {detail}"
                    self._quarantine_unlocked(msg)
                    raise sqlite3.OperationalError(msg) from rb_err
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if kind.identity_column is not None:
                    assert resolved_id is not None
                    self._check_fresh_identity_unlocked(kind, key, resolved_id)
                self._conn.execute(
                    f"INSERT INTO {kind.attempts_table}({', '.join(cols)}) VALUES({placeholders}) "
                    f"ON CONFLICT({kind.key_column}) DO UPDATE SET {updates}",
                    values,
                )
                if on_staged is not None:
                    on_staged()
                self._conn.commit()
            except BaseException as exc:
                rollback_error = None
                if self._conn.in_transaction:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error as rb_exc:
                        rollback_error = rb_exc
                if rollback_error is not None or self._conn.in_transaction:
                    detail = (
                        str(rollback_error) if rollback_error is not None
                        else "transaction remained open"
                    )
                    msg = f"progress store quarantined after unconfirmed rollback: {detail}"
                    self._quarantine_unlocked(msg)
                    raise sqlite3.OperationalError(msg) from (rollback_error or exc)
                raise
            return True

    def start_attempt(
        self,
        kind: AttemptKind,
        key,
        started_at: float,
        window_s: int,
        *,
        attempt_id: str | None = None,
        **extra,
    ) -> bool:
        """Upsert one attempt row (including any kind.attempt_columns) without altering passes."""
        self._validate_attempt_extra(kind, extra)
        resolved_id = self._resolve_attempt_id(kind, attempt_id, generate=True)
        cols = [kind.key_column, "started_at", "window_s"]
        update_cols = ["started_at", "window_s"]
        val_list: list = [key, started_at, window_s]
        if kind.identity_column is not None:
            cols.append(kind.identity_column)
            update_cols.append(kind.identity_column)
            val_list.append(resolved_id)
        cols.extend(kind.attempt_columns)
        update_cols.extend(kind.attempt_columns)
        val_list.extend(extra[col] for col in kind.attempt_columns)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{col}=excluded.{col}" for col in update_cols)
        values = tuple(val_list)
        with self._lock:
            self._require_usable_unlocked()
            if self._closed:
                return False
            if kind.identity_column is not None:
                assert resolved_id is not None
                self._check_fresh_identity_unlocked(kind, key, resolved_id)
            self._conn.execute(
                f"INSERT INTO {kind.attempts_table}({', '.join(cols)}) VALUES({placeholders}) "
                f"ON CONFLICT({kind.key_column}) DO UPDATE SET {updates}",
                values,
            )
            self._conn.commit()
            return True

    def restore_attempt(self, kind: AttemptKind, key, previous: dict | None) -> bool:
        """Restore a key's previous attempt row (including extra columns), or delete if None."""
        extra_needed = (
            (kind.identity_column, *kind.attempt_columns)
            if kind.identity_column is not None
            else kind.attempt_columns
        )
        if previous is not None:
            needed = ("started_at", "window_s", *extra_needed)
            missing = [col for col in needed if col not in previous]
            if missing:
                raise ValueError(
                    f"attempt kind {kind.kind!r} previous attempt missing columns: "
                    f"{', '.join(missing)}"
                )
        with self._lock:
            self._require_usable_unlocked()
            if self._closed:
                return False
            if previous is None:
                self._conn.execute(
                    f"DELETE FROM {kind.attempts_table} WHERE {kind.key_column}=?",
                    (key,),
                )
            else:
                cols = [kind.key_column, "started_at", "window_s", *extra_needed]
                update_cols = ["started_at", "window_s", *extra_needed]
                placeholders = ", ".join("?" for _ in cols)
                updates = ", ".join(f"{col}=excluded.{col}" for col in update_cols)
                values = (
                    key,
                    previous["started_at"],
                    previous["window_s"],
                    *(previous[col] for col in extra_needed),
                )
                self._conn.execute(
                    f"INSERT INTO {kind.attempts_table}({', '.join(cols)}) "
                    f"VALUES({placeholders}) "
                    f"ON CONFLICT({kind.key_column}) DO UPDATE SET {updates}",
                    values,
                )
            self._conn.commit()
            return True

    def attempt(self, kind: AttemptKind, key) -> dict:
        """Return a key's current attempt, its own pass(es), newest pass overall, and pass count."""
        attempt_cols_list = [kind.key_column, "started_at", "window_s"]
        if kind.identity_column is not None:
            attempt_cols_list.append(kind.identity_column)
        attempt_cols_list.extend(kind.attempt_columns)
        attempt_cols = ", ".join(attempt_cols_list)
        pass_cols_list = [kind.key_column, "started_at"]
        if kind.identity_column is not None:
            pass_cols_list.append(kind.identity_column)
        if kind.slot_column is not None:
            pass_cols_list.append(kind.slot_column)
        pass_cols_list.extend(kind.pass_columns)
        pass_cols = ", ".join(pass_cols_list)
        with self._lock:
            self._require_usable_unlocked()
            if self._closed:
                return {
                    "attempt": None,
                    "attempt_pass": None,
                    "attempt_passes": [],
                    "latest_pass": None,
                    "passes": 0,
                }
            attempt_row = self._conn.execute(
                f"SELECT {attempt_cols} FROM {kind.attempts_table} WHERE {kind.key_column}=?",
                (key,),
            ).fetchone()
            attempt_pass_rows = []
            if attempt_row is not None:
                where_parts = [f"{kind.key_column}=?", "started_at=?"]
                where_vals: list = [key, attempt_row["started_at"]]
                if kind.identity_column is not None:
                    where_parts.append(f"{kind.identity_column}=?")
                    where_vals.append(attempt_row[kind.identity_column])
                where_sql = " AND ".join(where_parts)
                if kind.slot_column is not None:
                    attempt_pass_rows = self._conn.execute(
                        f"SELECT {pass_cols} FROM {kind.passes_table} "
                        f"WHERE {where_sql} "
                        f"ORDER BY {kind.slot_column}",
                        tuple(where_vals),
                    ).fetchall()
                else:
                    row = self._conn.execute(
                        f"SELECT {pass_cols} FROM {kind.passes_table} WHERE {where_sql}",
                        tuple(where_vals),
                    ).fetchone()
                    if row is not None:
                        attempt_pass_rows = [row]
            pass_row = self._conn.execute(
                f"SELECT {pass_cols} FROM {kind.passes_table} WHERE {kind.key_column}=? "
                f"ORDER BY passed_at DESC, started_at DESC LIMIT 1",
                (key,),
            ).fetchone()
            count = self._conn.execute(
                f"SELECT COUNT(*) FROM {kind.passes_table} WHERE {kind.key_column}=?",
                (key,),
            ).fetchone()[0]
        attempt_passes = [self._decode_pass_row(kind, r) for r in attempt_pass_rows]
        attempt_pass = attempt_passes[0] if attempt_passes else None
        latest = self._decode_pass_row(kind, pass_row) if pass_row else None
        return {
            "attempt": dict(attempt_row) if attempt_row else None,
            "attempt_pass": attempt_pass,
            "attempt_passes": attempt_passes,
            "latest_pass": latest,
            "passes": count,
        }

    def record_attempt_pass(
        self,
        kind: AttemptKind,
        key,
        started_at: float,
        slot,
        pass_row: dict,
        *,
        attempt_id: str | None = None,
    ) -> bool:
        """Insert one pass row atomically only if the matching attempt still exists."""
        sql, params = self._prepare_guarded_pass_insert(
            kind, key, started_at, slot, pass_row, attempt_id=attempt_id,
        )
        with self._lock:
            self._require_usable_unlocked()
            if self._closed:
                return False
            with self._conn:
                return bool(self._conn.execute(sql, params).rowcount)

    def credit_attempts(self, kind: AttemptKind, stamps, qualifies) -> list[dict]:
        """Credit open live_reception attempts in one transaction using guarded insert-once."""
        if kind.credit != "live_reception":
            raise ValueError(
                f"credit_attempts only accepts live_reception kinds, got {kind.credit!r}"
            )
        attempt_cols = [kind.key_column, "started_at", "window_s"]
        if kind.identity_column is not None:
            attempt_cols.append(kind.identity_column)
        attempt_cols.extend(kind.attempt_columns)
        with self._lock:
            self._require_usable_unlocked()
            if self._closed:
                return []
            credited: list[dict] = []
            with self._conn:
                if kind.slot_column is None:
                    col_sql = ", ".join(f"c.{col}" for col in attempt_cols)
                    id_join = (
                        f" AND p.{kind.identity_column} = c.{kind.identity_column}"
                        if kind.identity_column is not None
                        else ""
                    )
                    open_attempts = [
                        dict(row) for row in self._conn.execute(
                            f"SELECT {col_sql} "
                            f"FROM {kind.attempts_table} c "
                            f"WHERE NOT EXISTS ("
                            f"  SELECT 1 FROM {kind.passes_table} p "
                            f"  WHERE p.{kind.key_column} = c.{kind.key_column} "
                            f"    AND p.started_at = c.started_at{id_join}"
                            f") ORDER BY c.{kind.key_column}"
                        ).fetchall()
                    ]
                    if not open_attempts:
                        return []
                    for stamp in stamps:
                        if not open_attempts:
                            break
                        remaining: list[dict] = []
                        for attempt in open_attempts:
                            if qualifies(attempt, stamp):
                                pass_row = {
                                    "passed_at": stamp["first_ts"],
                                    "instrument": stamp["instrument"],
                                    "provenance": stamp["provenance"],
                                    "digest": stamp["digest"],
                                }
                                aid = (
                                    attempt[kind.identity_column]
                                    if kind.identity_column is not None
                                    else None
                                )
                                inserted = self._insert_attempt_pass_unlocked(
                                    kind,
                                    attempt[kind.key_column],
                                    attempt["started_at"],
                                    None,
                                    pass_row,
                                    attempt_id=aid,
                                )
                                if inserted:
                                    item = {
                                        kind.key_column: attempt[kind.key_column],
                                        "started_at": attempt["started_at"],
                                        **pass_row,
                                        "event_id": stamp.get("event_id"),
                                    }
                                    if kind.identity_column is not None:
                                        item[kind.identity_column] = aid
                                    credited.append(item)
                            else:
                                remaining.append(attempt)
                        open_attempts = remaining
                else:
                    col_sql = ", ".join(attempt_cols)
                    open_attempts = [
                        dict(row) for row in self._conn.execute(
                            f"SELECT {col_sql} FROM {kind.attempts_table} "
                            f"ORDER BY {kind.key_column}"
                        ).fetchall()
                    ]
                    if not open_attempts:
                        return []
                    id_join = (
                        f" AND p.{kind.identity_column} = c.{kind.identity_column}"
                        if kind.identity_column is not None
                        else ""
                    )
                    passed_slots = {
                        (row[0], row[1], row[2])
                        for row in self._conn.execute(
                            f"SELECT p.{kind.key_column}, p.started_at, p.{kind.slot_column} "
                            f"FROM {kind.passes_table} p "
                            f"JOIN {kind.attempts_table} c "
                            f"  ON p.{kind.key_column} = c.{kind.key_column} "
                            f" AND p.started_at = c.started_at{id_join}"
                        ).fetchall()
                    }
                    for stamp in stamps:
                        for attempt in open_attempts:
                            slot = qualifies(attempt, stamp)
                            if slot is None or slot is False:
                                continue
                            if slot is True:
                                raise TypeError(
                                    f"slotted attempt kind {kind.kind!r} qualifies() "
                                    f"must return a slot value or None, not True"
                                )
                            slot_key = (attempt[kind.key_column], attempt["started_at"], slot)
                            if slot_key in passed_slots:
                                continue
                            pass_row = {
                                "passed_at": stamp["first_ts"],
                                "instrument": stamp["instrument"],
                                "provenance": stamp["provenance"],
                                "digest": stamp["digest"],
                            }
                            aid = (
                                attempt[kind.identity_column]
                                if kind.identity_column is not None
                                else None
                            )
                            inserted = self._insert_attempt_pass_unlocked(
                                kind,
                                attempt[kind.key_column],
                                attempt["started_at"],
                                slot,
                                pass_row,
                                attempt_id=aid,
                            )
                            passed_slots.add(slot_key)
                            if inserted:
                                item = {
                                    kind.key_column: attempt[kind.key_column],
                                    "started_at": attempt["started_at"],
                                    kind.slot_column: slot,
                                    **pass_row,
                                    "event_id": stamp.get("event_id"),
                                }
                                if kind.identity_column is not None:
                                    item[kind.identity_column] = aid
                                credited.append(item)
            return credited

    def start_challenge(self, unit_id: int, started_at: float, window_s: int) -> bool:
        """Upsert a unit's current live-find attempt without altering earned passes."""
        return self.start_attempt(CHALLENGE_KIND, unit_id, started_at, window_s)

    def restore_challenge(self, unit_id: int, previous: dict | None) -> bool:
        """Restore a unit's previous live-find attempt, or remove it if none existed."""
        return self.restore_attempt(CHALLENGE_KIND, unit_id, previous)

    def challenge(self, unit_id: int) -> dict:
        """Return a unit's current attempt, its own pass, newest pass overall, and pass count."""
        saved = self.attempt(CHALLENGE_KIND, unit_id)
        return {
            "attempt": saved["attempt"],
            "attempt_pass": saved["attempt_pass"],
            "latest_pass": saved["latest_pass"],
            "passes": saved["passes"],
        }

    def credit_challenges(self, stamps, qualifies) -> list[dict]:
        """Credit open live-find attempts in one transaction using the first qualifying stamp."""
        return self.credit_attempts(CHALLENGE_KIND, stamps, qualifies)

    def solo_calls(self, solo_id: str, started_at: float) -> list[dict]:
        """Return all legal calls recorded for one solo attempt, ordered by band_slot."""
        with self._lock:
            if self._closed:
                return []
            rows = self._conn.execute(
                "SELECT solo_id, started_at, band_slot, call, why_key, call_correct, "
                "why_correct, answered_at, item_revision "
                "FROM solo_calls WHERE solo_id=? AND started_at=? ORDER BY band_slot",
                (solo_id, started_at),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_solo_complete_pass_ts(self, solo_id: str, leg_count: int) -> float | None:
        """Newest answered_at where all `leg_count` legs passed both reception and legal call."""
        if leg_count <= 0:
            return None
        with self._lock:
            if self._closed:
                return None
            row = self._conn.execute(
                "SELECT MAX(c.answered_at) AS done_at "
                "FROM solo_calls c "
                "JOIN solo_passes p "
                "  ON p.solo_id = c.solo_id "
                " AND p.started_at = c.started_at "
                " AND p.band_slot = c.band_slot "
                "WHERE c.solo_id=? AND c.call_correct=1 AND c.why_correct=1 "
                "GROUP BY c.started_at "
                "HAVING COUNT(DISTINCT c.band_slot) >= ? "
                "ORDER BY done_at DESC LIMIT 1",
                (solo_id, int(leg_count)),
            ).fetchone()
        return float(row["done_at"]) if row and row["done_at"] is not None else None

    def record_solo_call(
        self,
        solo_id: str,
        started_at: float,
        band_slot: int,
        *,
        call: str,
        why_key: str,
        call_correct: bool,
        why_correct: bool,
        answered_at: float,
        item_revision: str,
    ) -> bool:
        """Insert one solo legal call (insert-once via ON CONFLICT DO NOTHING)."""
        with self._lock:
            if self._closed:
                return False
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO solo_calls("
                    "solo_id, started_at, band_slot, call, why_key, call_correct, "
                    "why_correct, answered_at, item_revision"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (
                        solo_id,
                        started_at,
                        band_slot,
                        call,
                        why_key,
                        1 if call_correct else 0,
                        1 if why_correct else 0,
                        answered_at,
                        item_revision,
                    ),
                )
            return bool(cur.rowcount)

    def delete_solo_call(self, solo_id: str, started_at: float, band_slot: int) -> bool:
        """Remove a newly inserted solo call when its event log write fails so resubmit works."""
        with self._lock:
            if self._closed:
                return False
            with self._conn:
                cur = self._conn.execute(
                    "DELETE FROM solo_calls WHERE solo_id=? AND started_at=? AND band_slot=?",
                    (solo_id, started_at, band_slot),
                )
            return bool(cur.rowcount)

    def enroll_recall(self, lesson_ids, *, now: float) -> None:
        """Enrol newly eligible completed lessons into box 1 without overwriting existing rows."""
        ids = [int(lid) for lid in lesson_ids]
        if not ids:
            return
        ts = float(now)
        with self._lock:
            self._conn.executemany(
                "INSERT INTO retrieval(lesson_id, box, last_seen, seen, correct, last_choice) "
                "VALUES(?, 1, ?, 0, 0, NULL) ON CONFLICT(lesson_id) DO NOTHING",
                [(lid, ts) for lid in ids],
            )
            self._conn.commit()

    def get_retrieval(self, lesson_ids) -> dict[int, dict]:
        """Current retrieval rows for the given lesson IDs, keyed by lesson_id."""
        ids = [int(lid) for lid in lesson_ids]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                "SELECT lesson_id, box, last_seen, seen, correct, last_choice "
                f"FROM retrieval WHERE lesson_id IN ({placeholders})",
                ids,
            ).fetchall()
        return {row["lesson_id"]: dict(row) for row in rows}

    def record_recall(self, lesson_id: int, choice: str, *, correct: bool, now: float) -> int:
        """Record one recall answer and return its new box, refusing if missing or not yet due."""
        choice = (choice or "").strip()
        if not choice:
            raise ValueError("a recall answer cannot be blank")
        ts = float(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT box, last_seen FROM retrieval WHERE lesson_id=?",
                (int(lesson_id),),
            ).fetchone()
            if row is None or not retrieval.is_due(row["box"], row["last_seen"], now=ts):
                raise RecallNotDue(f"lesson {lesson_id} recall card is not due")
            new_box = retrieval.next_box(row["box"], correct)
            self._conn.execute(
                "UPDATE retrieval SET box=?, last_seen=?, seen=seen + 1, "
                "correct=correct + ?, last_choice=? WHERE lesson_id=?",
                (new_box, ts, 1 if correct else 0, choice, int(lesson_id)),
            )
            self._conn.commit()
        return new_box

    def record_replay_seen(self, instrument, *, recording, digest, ts) -> bool:
        """Count one replay with output and retain the first recording as its evidence."""
        with self._lock:
            if self._closed:
                return False
            with self._conn:
                self._conn.execute(
                    "INSERT INTO replays_seen(instrument, first_ts, count, recording, digest) "
                    "VALUES(?, ?, 1, ?, ?) ON CONFLICT(instrument) DO UPDATE SET "
                    "count=replays_seen.count + 1",
                    (instrument, ts, recording, digest),
                )
            return True

    def replays_seen(self) -> dict:
        """Read the separate replay ledger, keyed by instrument."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT instrument, first_ts, count, recording, digest FROM replays_seen"
            ).fetchall()
        return {row["instrument"]: {key: row[key] for key in ("first_ts", "count", "recording",
                                                             "digest")} for row in rows}

    def close(self) -> None:
        """Close the database and fence any late reception writes under the same lock."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()
