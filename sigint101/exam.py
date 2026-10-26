# ABOUTME: Technician exam-trainer engine — verified NCVEC pool loader, Leitner spaced
# ABOUTME: repetition over question ids, and real-shape 35-question practice exams + scoring.

from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# The committed, verified pool. A wrong pool teaches wrong answers, so the file is pinned
# to the NCVEC 2026-2030 Technician release (effective 2026-07-01) with the 2026-02-19
# errata already applied; see its `source` block for provenance.
POOL_PATH = Path(__file__).parent / "exam_data" / "technician-2026-2030.json"

# The published NCVEC pool size. The loader never invents questions to reach it; verify()
# is what refuses a pool that does not match, so the trainer can ship disabled rather than
# teach from a short or stale pool.
EXPECTED_COUNT = 409

# The real Element 2 exam: 35 questions, one drawn from each of the pool's 35 question
# groups, pass at 26 correct (74%). These are FCC/NCVEC facts, not tunables.
EXAM_SIZE = 35
PASS_MARK = 26

# The four questions the 2026-02-19 errata reworded. verify() asserts each carries its
# corrected text AND answer, because the pre-errata wording keeps the same answer letters —
# only the question text proves the errata was applied rather than the stale Dec-2025 pool.
ERRATA = {
    "T1C01": (
        "D",
        "For which classes of amateur radio licenses does the FCC currently issue "
        "new licenses?",
    ),
    "T5A05": ("A", "A difference in which of the following causes electron flow?"),
    "T7A09": (
        "B",
        "What is the function of the switch which selects either SSB or CW-FM on some "
        "VHF power amplifiers?",
    ),
    "T0A10": (
        "A",
        "What hazard exists when rapidly charging or discharging an unprotected battery?",
    ),
}

# Leitner boxes 1..5. A question climbs one box per correct recall and drops to box 1 on a
# miss; each box waits longer before the question comes due again. Box 1 is due immediately,
# so a freshly missed or never-seen question is always offered.
LEITNER_BOXES = 5
_DAY = 86400
BOX_INTERVALS = (0, _DAY, 3 * _DAY, 7 * _DAY, 30 * _DAY)


class PoolError(RuntimeError):
    """Raised when the pool file is missing, empty, or fails its correctness guard.

    The console is expected to catch this and ship the trainer disabled rather than
    present a pool it cannot vouch for — teaching a wrong answer is worse than teaching none.
    """


@dataclass(frozen=True)
class Question:
    """One pool question. Frozen because the pool is reference data, not runtime state."""

    id: str
    subelement: str
    group: str
    question: str
    options: dict
    answer: str
    fcc_refs: tuple
    figure: str | None

    @property
    def correct_option(self) -> str:
        """The text of the correct choice, resolved through the answer letter."""
        return self.options[self.answer]


# Subelements run T1..T9 then T0 (Safety) last, the order the syllabus prints them.
def _subelement_rank(subelement: str) -> int:
    digit = subelement[1]
    return 10 if digit == "0" else int(digit)


class Pool:
    """The loaded question pool plus its provenance metadata.

    Holds questions indexed by id and by group so the engine can look a question up and
    draw one question per group without re-scanning the list each time.
    """

    def __init__(self, questions, meta: dict):
        self.questions = tuple(questions)
        self.meta = meta
        self._by_id = {q.id: q for q in self.questions}
        self._by_group: dict = {}
        for q in self.questions:
            self._by_group.setdefault(q.group, []).append(q)

    @property
    def count(self) -> int:
        return len(self.questions)

    def get(self, question_id: str):
        """The question with this id, or None if the pool does not carry it."""
        return self._by_id.get(question_id)

    def groups(self) -> list:
        """Every question group, in exam order (subelement, then group letter)."""
        return sorted(self._by_group, key=lambda g: (_subelement_rank(g[:2]), g))

    def by_group(self, group: str) -> list:
        """The questions in one group."""
        return list(self._by_group.get(group, []))

    def blueprint(self) -> dict:
        """How many questions each subelement contributes to a practice exam.

        One question is drawn per group, so a subelement's share is its group count — which
        is exactly the NCVEC exam design (T1 six, T2 three, ... summing to 35).
        """
        counts: dict = {}
        for group in self.groups():
            sub = group[:2]
            counts[sub] = counts.get(sub, 0) + 1
        return counts

    def verify(self) -> bool:
        """Correctness guard: the pool is the official size and the errata are applied.

        Raises PoolError on any mismatch so a caller can disable the trainer. Returns True
        only when every check passes.
        """
        if self.count != EXPECTED_COUNT:
            raise PoolError(
                f"pool has {self.count} questions, expected {EXPECTED_COUNT}"
            )
        for qid, (answer, text) in ERRATA.items():
            q = self.get(qid)
            if q is None:
                raise PoolError(f"errata question {qid} is missing from the pool")
            if q.answer != answer:
                raise PoolError(
                    f"errata question {qid} has answer {q.answer!r}, expected {answer!r}"
                )
            if q.question != text:
                raise PoolError(
                    f"errata question {qid} does not carry its corrected text "
                    "(pre-errata pool?)"
                )
        return True


def load_pool(path=None) -> Pool:
    """Load the pool from JSON. Honest about trouble, never fabricating questions.

    A missing file or an empty question list raises PoolError rather than returning a
    placeholder pool: the trainer has no questions to teach from, and must say so.
    """
    path = Path(path) if path is not None else POOL_PATH
    if not path.exists():
        raise PoolError(f"pool file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("questions") or []
    if not raw:
        raise PoolError(f"pool file carries no questions: {path}")
    questions = [
        Question(
            id=q["id"],
            subelement=q["subelement"],
            group=q["group"],
            question=q["question"],
            options=dict(q["options"]),
            answer=q["answer"],
            fcc_refs=tuple(q.get("fcc_refs", ())),
            figure=q.get("figure"),
        )
        for q in raw
    ]
    meta = {k: v for k, v in data.items() if k != "questions"}
    return Pool(questions, meta)


class ExamStore:
    """Per-question Leitner state, persisted in SQLite like the lesson progress store.

    SQLite rather than a flat file for the same reason the console does: a kiosk that loses
    power in the field must not lose a learner's study history to a half-written row.
    """

    # Lives under data/, which .gitignore keeps out of the repo so study history is never
    # committed. Tests pass an explicit temp path and so never touch this shared file.
    DEFAULT_DB_PATH = "data/exam.db"

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS srs (
        question_id TEXT PRIMARY KEY,
        box         INTEGER NOT NULL DEFAULT 1,
        last_seen   REAL,
        seen        INTEGER NOT NULL DEFAULT 0,
        correct     INTEGER NOT NULL DEFAULT 0
    );
    """

    def __init__(self, db_path=None):
        self.db_path = str(db_path) if db_path is not None else self.DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + a lock, matching progress.py: the FastAPI threadpool
        # touches this from a different thread than the one that opened it.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(self.SCHEMA)
            self._conn.commit()

    def record(self, question_id: str, correct: bool, *, clock=time.time) -> int:
        """Record one answer and return the question's new box.

        Correct recall promotes one box (capped at the top box); a miss drops straight to
        box 1 — the Leitner rule that a forgotten card is treated as new again.
        """
        ts = clock()
        with self._lock:
            row = self._conn.execute(
                "SELECT box FROM srs WHERE question_id=?", (question_id,)
            ).fetchone()
            current = row["box"] if row else 1
            new_box = min(current + 1, LEITNER_BOXES) if correct else 1
            # excluded is SQLite's name for the row that collided; seen/correct accumulate
            # off the existing row so the history survives repeated answers.
            self._conn.execute(
                "INSERT INTO srs(question_id, box, last_seen, seen, correct) "
                "VALUES(?, ?, ?, 1, ?) "
                "ON CONFLICT(question_id) DO UPDATE SET "
                "box=excluded.box, last_seen=excluded.last_seen, "
                "seen=srs.seen+1, correct=srs.correct+excluded.correct",
                (question_id, new_box, ts, 1 if correct else 0),
            )
            self._conn.commit()
        return new_box

    def box_of(self, question_id: str) -> int:
        """The question's current box; 1 for a question never answered."""
        with self._lock:
            row = self._conn.execute(
                "SELECT box FROM srs WHERE question_id=?", (question_id,)
            ).fetchone()
        return row["box"] if row else 1

    def state(self, question_id: str):
        """Everything stored for one question, or None if it has never been answered."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM srs WHERE question_id=?", (question_id,)
            ).fetchone()
        return dict(row) if row else None

    def due(self, question_ids, *, now) -> list:
        """Which of these questions are due at `now`, weakest (lowest box) first.

        A never-seen question is due immediately; a seen one is due once its box interval
        has elapsed since it was last answered. Ordering box-ascending makes the queue a
        study order: the questions most at risk of being forgotten surface first.
        """
        ranked = []
        with self._lock:
            for qid in question_ids:
                row = self._conn.execute(
                    "SELECT box, last_seen FROM srs WHERE question_id=?", (qid,)
                ).fetchone()
                if row is None:
                    ranked.append((1, qid))
                    continue
                box = row["box"]
                last_seen = row["last_seen"]
                interval = BOX_INTERVALS[box - 1]
                if last_seen is None or now - last_seen >= interval:
                    ranked.append((box, qid))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [qid for _, qid in ranked]

    def close(self) -> None:
        """Close the database so the file is left clean on shutdown."""
        with self._lock:
            self._conn.close()


def build_exam(pool: Pool, *, rng=None) -> list:
    """Draw a practice exam: one random question from each group, order shuffled.

    One-per-group is the real Element 2 construction, so the result is always EXAM_SIZE
    questions spread across the subelements exactly as the pool's blueprint prescribes.
    """
    rng = rng or random.Random()
    sheet = [rng.choice(pool.by_group(group)) for group in pool.groups()]
    rng.shuffle(sheet)
    return sheet


@dataclass(frozen=True)
class AnswerOutcome:
    """How one answer landed: what was chosen, what was right, and whether they matched."""

    question_id: str
    chosen: str | None
    answer: str
    is_correct: bool


@dataclass(frozen=True)
class ExamResult:
    """The scored outcome of a practice exam."""

    total: int
    correct: int
    passed: bool
    detail: tuple


def score_exam(questions, answers: dict) -> ExamResult:
    """Score a practice exam. `answers` maps question id to the chosen letter.

    An unanswered question counts as wrong — never silently correct — so a blank sheet
    scores zero and the pass line stays honest.
    """
    detail = []
    for q in questions:
        chosen = answers.get(q.id)
        detail.append(AnswerOutcome(q.id, chosen, q.answer, chosen == q.answer))
    correct = sum(1 for d in detail if d.is_correct)
    return ExamResult(
        total=len(questions),
        correct=correct,
        passed=correct >= PASS_MARK,
        detail=tuple(detail),
    )
