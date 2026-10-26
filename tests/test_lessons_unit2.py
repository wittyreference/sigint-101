# ABOUTME: Tests the Unit 2 lesson pack (M5 "The One Rule") loads: a no-RF knowledge lesson that
# ABOUTME: declares no hardware or panels, keeps the six-stage intro shape, and sits in Unit 2.

from pathlib import Path

import pytest

from sigint101.lessons import REQUIRED_STAGES, load_lessons
from sigint101.units import unit_for_lesson

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lessons(monkeypatch):
    """Load every pack in lesson_packs/ the way the console does, from the repo root."""
    monkeypatch.chdir(REPO_ROOT)
    return load_lessons("lesson_packs")


def test_m5_the_rules_loads(lessons):
    assert 5 in lessons, "M5 (the legal rules) is missing from lesson_packs/"
    assert lessons[5].title == "The One Rule"


def test_m5_is_a_six_stage_intro_with_real_text(lessons):
    lesson = lessons[5]
    assert lesson.type == "intro"
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"M5 stage {stage!r} must carry real teaching text"


def test_m5_needs_no_radio(lessons):
    # The rules lesson teaches law, not RF: no hardware and no panels, so the console renders it as
    # text with no waterfall (see the console template's needs_hardware guard).
    lesson = lessons[5]
    assert lesson.hardware == []
    assert lesson.panels == []
    assert lesson.needs_hardware is False
    assert lesson.requires_tx is False


def test_m5_offers_well_formed_predict_choices(lessons):
    lesson = lessons[5]
    assert lesson.choices, "M5 must offer predict choices (keyboard-free gate)"
    for choice in lesson.choices:
        assert choice.get("key") and choice.get("label")


def test_m5_belongs_to_unit_2(lessons):
    assert unit_for_lesson(5).id == 2
