# ABOUTME: Pure Leitner scheduler for pre-lesson retrieval cards (1, 3, 7, 14, and 30 day waits).
# ABOUTME: Holds no database connection and never serves the lesson being opened.

from __future__ import annotations

DAY_S = 86_400

# Every box waits at least a full day so a newly completed lesson never quizzes the learner
# seconds after finishing and a missed card never loops on the same visit.
BOX_INTERVALS = (1 * DAY_S, 3 * DAY_S, 7 * DAY_S, 14 * DAY_S, 30 * DAY_S)
MAX_BOX = len(BOX_INTERVALS)
MAX_CARDS = 2


def next_box(box: int, correct: bool) -> int:
    """Promote one box on a right answer (capped at 5); drop to box 1 on a miss."""
    current = min(max(1, int(box)), MAX_BOX)
    return min(current + 1, MAX_BOX) if correct else 1


def is_due(box: int, last_seen: float, *, now: float) -> bool:
    """True once this box's wait has elapsed since `last_seen`; false if the clock stepped back."""
    current = min(max(1, int(box)), MAX_BOX)
    return (now - last_seen) >= BOX_INTERVALS[current - 1]


def due(rows: dict, *, now: float, exclude: int | None = None) -> list[int]:
    """Due lesson IDs ordered by weakest box first, then oldest `last_seen`, then lesson ID."""
    ranked = []
    for lesson_id, row in rows.items():
        if exclude is not None and lesson_id == exclude:
            continue
        box = row["box"]
        last_seen = row["last_seen"]
        if is_due(box, last_seen, now=now):
            ranked.append((box, last_seen, lesson_id))
    ranked.sort()
    return [lesson_id for _, _, lesson_id in ranked]
