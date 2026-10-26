# ABOUTME: Tests the Unit 1 lesson packs (M1-M4) load together via load_lessons: all four
# ABOUTME: present, each a six-stage intro with its declared panel and well-formed predict choices.

from pathlib import Path

import pytest

from sigint101.lessons import REQUIRED_STAGES, load_lessons

# load_lessons takes a path relative to the working directory. The acceptance gate
# (make selftest) runs pytest from the repo root, but pin the cwd here so the test is correct
# no matter where pytest is invoked, while still calling the loader exactly as the console does.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The four Unit 1 packs and the extra panel each one turns on beyond the waterfall baseline:
# M1 needs no extra panel; M2 the gain panel (SNR), M3 provenance (baseline), M4 ppm (error).
EXPECTED_PANELS = {
    1: [],
    2: ["gain"],
    3: ["provenance"],
    4: ["ppm"],
}


@pytest.fixture
def unit1_lessons(monkeypatch):
    """Load every pack in lesson_packs/ the way the console does, from the repo root."""
    monkeypatch.chdir(REPO_ROOT)
    return load_lessons("lesson_packs")


def test_unit1_has_all_four_lessons(unit1_lessons):
    # Unit 1 is M1-M4; a missing id means a pack failed to load or was never written.
    for lesson_id in (1, 2, 3, 4):
        assert lesson_id in unit1_lessons, f"lesson {lesson_id} is missing from lesson_packs/"


def test_each_unit1_lesson_is_a_six_stage_intro(unit1_lessons):
    # Every Unit 1 pack teaches a concept from a blank slate, so it is an intro lesson and the
    # console walks all six stages in order; any blank stage would strand a beginner mid-lesson.
    for lesson_id in (1, 2, 3, 4):
        lesson = unit1_lessons[lesson_id]
        assert lesson.type == "intro", f"lesson {lesson_id} must be an intro lesson"
        assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
        for stage in REQUIRED_STAGES["intro"]:
            assert lesson.stages[stage].strip(), (
                f"lesson {lesson_id} stage {stage!r} must carry real teaching text"
            )


def test_each_unit1_lesson_declares_its_panel(unit1_lessons):
    # Each pack turns on exactly the console panel its concept needs: gain for SNR (M2),
    # provenance for the baseline (M3), ppm for frequency error (M4); M1 needs none.
    for lesson_id, panels in EXPECTED_PANELS.items():
        assert unit1_lessons[lesson_id].panels == panels


def test_every_pack_choice_has_a_key_and_a_label(unit1_lessons):
    # Guard against the past bug: predict choices once used `value` instead of `key`, which the
    # console's loader cannot record a tap against. Every pack carries predict choices, and every
    # entry must have both a non-empty key and a non-empty label.
    for lesson_id, lesson in unit1_lessons.items():
        assert lesson.choices, f"lesson {lesson_id} must offer predict choices"
        for choice in lesson.choices:
            assert choice.get("key"), f"lesson {lesson_id} has a choice missing 'key'"
            assert choice.get("label"), f"lesson {lesson_id} has a choice missing 'label'"
