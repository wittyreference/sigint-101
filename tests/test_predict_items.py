# ABOUTME: Contract test over the five predicts the 11-03 item review found BROKEN (L1, L2, L3, L7,
# ABOUTME: L8): one defensible key each, a short stem, and a try that doesn't do the predict's job.

from pathlib import Path

import pytest

from sigint101.lessons import load_lessons

PACK_DIR = Path(__file__).resolve().parent.parent / "lesson_packs"
LESSONS = load_lessons(PACK_DIR)

# docs/audit/2026-11-03-item-review.md §2: K, T or H failed on these five, so a careful learner
# could be marked MISSED for a defensible answer. Their rewrites (§3) each name one key.
REWRITTEN = (1, 2, 3, 7, 8)

# The review's rewrites run about 40 words: one situation, three options, "Lock it in."
STEM_MAX_WORDS = 40


@pytest.mark.parametrize("lesson_id", REWRITTEN)
def test_a_rewritten_predict_names_one_key(lesson_id):
    # A two-chip key leaves "it depends" as a safe hedge and only one chip that can be wrong
    # (review §1.3, cause 4).
    answer = LESSONS[lesson_id].answer
    assert len(answer) == 1, f"lesson {lesson_id} accepts {answer}; name the one defensible chip"


@pytest.mark.parametrize("lesson_id", REWRITTEN)
def test_a_rewritten_predict_stem_stays_short(lesson_id):
    words = len(LESSONS[lesson_id].stages["predict"].split())
    assert words <= STEM_MAX_WORDS, (
        f"lesson {lesson_id} predict is {words} words; keep it to {STEM_MAX_WORDS}")


def test_the_gain_try_stops_short_of_the_setting_the_predict_asks_about():
    # The predict asks what the floor does at maximum gain. A try that already sends the learner
    # to maximum turns the prediction into a report of a reading they have just taken.
    assert "maximum" not in LESSONS[2].stages["try"].lower()
