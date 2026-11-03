# ABOUTME: Contract test over the shipped lesson packs' reading load: the explain fits a short
# ABOUTME: reveal, and narration under a live instrument stays short enough to read while watching.

from pathlib import Path

import pytest

from sigint101.lessons import load_lessons

PACK_DIR = Path(__file__).resolve().parent.parent / "lesson_packs"
LESSONS = load_lessons(PACK_DIR)

# The 800x480 panel holds about 50 words a screen. The explain scrolls, but the 11-02 curriculum
# audit (§6b, U1) found a median of 222 words, four to five screens: the answer, two or three
# lines of why and one line to carry out the door fit in about 120.
EXPLAIN_MAX_WORDS = 120

# M5 and M15 teach the law, and their legal sentences stay word for word as corrected after the
# audit (§5a X1, R1): the statute is the lesson there, so the explain gets room for it, not a trim.
LAW_EXPLAIN_MAX_WORDS = {5: 165, 15: 185}

# An instrument stage's narration reads in a thin band under the live panel while the learner is
# told to watch it: split attention. About 60 words keeps the instruction and drops the lecture.
NARRATION_MAX_WORDS = 60
INSTRUMENT_STAGES = ("demo", "try", "observe")


def _words(text: str) -> int:
    return len(text.split())


@pytest.mark.parametrize("lesson_id", sorted(LESSONS))
def test_every_explain_fits_a_short_reveal(lesson_id):
    words = _words(LESSONS[lesson_id].stages["explain"])
    limit = LAW_EXPLAIN_MAX_WORDS.get(lesson_id, EXPLAIN_MAX_WORDS)
    assert words <= limit, f"lesson {lesson_id} explain is {words} words; keep it to {limit}"


@pytest.mark.parametrize("lesson_id", sorted(i for i, lesson in LESSONS.items()
                                             if lesson.needs_hardware))
def test_every_instrument_narration_reads_while_watching(lesson_id):
    lesson = LESSONS[lesson_id]
    long = {stage: _words(lesson.stages[stage]) for stage in INSTRUMENT_STAGES
            if _words(lesson.stages[stage]) > NARRATION_MAX_WORDS}
    assert not long, (
        f"lesson {lesson_id} narration over {NARRATION_MAX_WORDS} words: {long}")
