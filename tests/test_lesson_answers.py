# ABOUTME: Contract test over the shipped lesson packs: every predict gate is scored, naming an
# ABOUTME: answer among its own chips and a short one-line verdict, and no chip label leaks it.

from pathlib import Path

import pytest

from sigint101.lessons import load_lessons

PACK_DIR = Path(__file__).resolve().parent.parent / "lesson_packs"
LESSONS = load_lessons(PACK_DIR)

# The verdict prints in the explain header above the reading, so it stays one short sentence.
VERDICT_MAX_CHARS = 150

# Chip labels the 11-02 curriculum audit (§6a, §8d R8) found giving the answer away before the
# commit. They must never come back: a label that argues its own case turns the predict gate into a
# reading test of the label.
LEAKY_LABELS = (
    "No — silence proves nothing",
    "No — red means look closer",
    "Yes — its mirror below the LO",
    "2.4 MHz — the full rate",
    "Illegal — transmitting needs a licence",
    "A bench: screen + storage",
)


@pytest.mark.parametrize("lesson_id", sorted(LESSONS))
def test_every_pack_names_an_answer_among_its_choices(lesson_id):
    lesson = LESSONS[lesson_id]
    assert lesson.answer, f"lesson {lesson_id} must name the right chip(s) in 'answer'"
    keys = {choice["key"] for choice in lesson.choices}
    assert set(lesson.answer) <= keys


@pytest.mark.parametrize("lesson_id", sorted(LESSONS))
def test_every_pack_carries_a_short_one_line_verdict(lesson_id):
    verdict = LESSONS[lesson_id].verdict
    assert verdict, f"lesson {lesson_id} must carry a one-line 'verdict'"
    assert "\n" not in verdict
    assert len(verdict) <= VERDICT_MAX_CHARS, (
        f"lesson {lesson_id} verdict is {len(verdict)} chars; keep it to one short sentence")


def test_the_right_chip_is_not_always_in_the_same_place():
    # Fourteen of the first 28 answers sat in chip A, a pattern a learner learns faster than the
    # radio. No one position may hold much more than its fair third.
    positions = {}
    for lesson in LESSONS.values():
        keys = [choice["key"] for choice in lesson.choices]
        spot = keys.index(lesson.answer[0])
        positions[spot] = positions.get(spot, 0) + 1
    assert max(positions.values()) <= 0.4 * len(LESSONS), positions


def test_no_pack_offers_a_label_that_gives_the_answer_away():
    labels = {choice["label"] for lesson in LESSONS.values() for choice in lesson.choices}
    assert not labels & set(LEAKY_LABELS)
