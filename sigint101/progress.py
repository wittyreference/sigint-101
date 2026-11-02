# ABOUTME: SQLite progress store — lessons opened and completed, and what the learner predicted.
# ABOUTME: Stdlib sqlite3, thread-safe for the kiosk (Starlette runs sync routes off-thread).

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

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

# The `kind` key for the learner's pre-observation guess. A constant because the predict
# gate and the lesson view must agree on the exact string, or the gate would never unlock.
PREDICTION = "prediction"


class Progress:
    """Where the learner's journey through a lesson is kept.

    SQLite rather than a file because the console is a kiosk that loses power in the
    field: a half-written row must not cost the learner their progress.
    """

    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + a lock: the FastAPI threadpool touches this from
        # a different thread than the one that opened it. The lock serializes access.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
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

    def record_prediction(self, lesson_id: int, body: str, *, clock=None) -> None:
        """Store what the learner expects to see, and open the lesson.

        Fails closed on a blank answer: an empty prediction is not a prediction, and
        accepting one would unlock the explanation without the learner committing first.
        """
        body = (body or "").strip()
        if not body:
            raise ValueError("a prediction cannot be blank")

        updated = clock() if clock else None
        with self._lock:
            # Both writes share one lock and one commit: a prediction that saved without
            # opening the lesson, or the reverse, would leave the console showing a state
            # the learner never reached. `excluded` is SQLite's name for the row that
            # collided, so this overwrites an earlier prediction rather than failing.
            self._conn.execute(
                "INSERT INTO responses(lesson_id, kind, body, updated) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(lesson_id, kind) DO UPDATE SET body=excluded.body, "
                "updated=excluded.updated",
                (lesson_id, PREDICTION, body, updated),
            )
            self._conn.execute(
                "INSERT INTO progress(lesson_id, opened, updated) VALUES(?, 1, ?) "
                "ON CONFLICT(lesson_id) DO UPDATE SET opened=1, updated=excluded.updated",
                (lesson_id, updated),
            )
            self._conn.commit()

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

    def close(self) -> None:
        """Close the database. Called from the app's shutdown so the file is left clean."""
        with self._lock:
            self._conn.close()
