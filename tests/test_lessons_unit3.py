# ABOUTME: Tests the Unit 3 lesson packs (M6-M7) load together via load_lessons: both present,
# ABOUTME: each a six-stage intro with every stage non-empty and well-formed predict choices.

from pathlib import Path

import pytest

from sigint101.lessons import REQUIRED_STAGES, load_lessons

# load_lessons takes a path relative to the working directory. The acceptance gate
# (make selftest) runs pytest from the repo root, but pin the cwd here so the test is correct
# no matter where pytest is invoked, while still calling the loader exactly as the console does.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Unit 3 is the antenna pair: M6 resonance, M7 build/aim. Both are intro packs.
UNIT3_IDS = (6, 7)


@pytest.fixture
def unit3_lessons(monkeypatch):
    """Load every pack in lesson_packs/ the way the console does, from the repo root."""
    monkeypatch.chdir(REPO_ROOT)
    return load_lessons("lesson_packs")


def test_unit3_has_both_lessons(unit3_lessons):
    # Unit 3 is M6-M7; a missing id means a pack failed to load or was never written.
    for lesson_id in UNIT3_IDS:
        assert lesson_id in unit3_lessons, f"lesson {lesson_id} is missing from lesson_packs/"


def test_each_unit3_lesson_is_a_six_stage_intro(unit3_lessons):
    # Both antenna packs teach a concept from a blank slate, so each is an intro lesson and the
    # console walks all six stages in order; any blank stage would strand a beginner mid-lesson.
    for lesson_id in UNIT3_IDS:
        lesson = unit3_lessons[lesson_id]
        assert lesson.type == "intro", f"lesson {lesson_id} must be an intro lesson"
        assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
        for stage in REQUIRED_STAGES["intro"]:
            assert lesson.stages[stage].strip(), (
                f"lesson {lesson_id} stage {stage!r} must carry real teaching text"
            )


def test_every_unit3_choice_has_a_key_and_a_label(unit3_lessons):
    # Guard against the past bug: predict choices once used `value` instead of `key`, which the
    # console's loader cannot record a tap against. Each antenna pack carries predict choices, and
    # every entry must have both a non-empty key and a non-empty label.
    for lesson_id in UNIT3_IDS:
        lesson = unit3_lessons[lesson_id]
        assert lesson.choices, f"lesson {lesson_id} must offer predict choices"
        for choice in lesson.choices:
            assert choice.get("key"), f"lesson {lesson_id} has a choice missing 'key'"
            assert choice.get("label"), f"lesson {lesson_id} has a choice missing 'label'"
