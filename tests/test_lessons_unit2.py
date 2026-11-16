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


# FCC Part 15 lets equipment that complies with it transmit with no individual licence (47 CFR
# 15.1(b), 15.201(b)), and only on its conditions. Wherever M5 names Part 15 it speaks of that
# equipment and names three of the conditions: the power limit and the antenna rule (47 CFR
# 15.247(b), 15.204(c)) and the rule against harmful interference (47 CFR 15.5(b)). The explain
# rules on the Meshtastic text, so it also names the region, Meshtastic's own setting.
PART_15_CONDITIONS = ("power", "antenna", "interference")


def _part_15_sentences(lesson, stage):
    """The sentences of a stage that name Part 15; the stage must have at least one."""
    sentences = [s for s in lesson.stages[stage].split(". ") if "Part 15" in s]
    assert sentences, f"M5 {stage} must name FCC Part 15 as the source of licence-free transmitting"
    return sentences


@pytest.mark.parametrize("stage", ["demo", "explain"])
def test_m5_names_part_15_together_with_its_conditions(lessons, stage):
    # An outside review found both stages naming Part 15 but giving power as the only condition
    # for sending the Meshtastic text without a licence. A sentence that names Part 15 must carry
    # all three conditions, so the learner never reads a power limit as the whole of the rule.
    for sentence in _part_15_sentences(lessons[5], stage):
        missing = [word for word in PART_15_CONDITIONS if word not in sentence]
        assert not missing, f"M5 {stage} names Part 15 without its {missing} conditions"


@pytest.mark.parametrize("stage,words", [("demo", ("equipment",)),
                                         ("explain", ("equipment", "region"))],
                         ids=["demo", "explain"])
def test_m5_ties_part_15_to_the_equipment_and_the_region(lessons, stage, words):
    # The licence-free sentence must say whose transmitting it is: the equipment's in both stages,
    # and in the explain a node set to its region. Only these two words are held, so the wording
    # around them stays free to change.
    for sentence in _part_15_sentences(lessons[5], stage):
        missing = [word for word in words if word not in sentence]
        assert not missing, f"M5 {stage} names Part 15 without speaking of the {missing}"


def test_m5_does_not_make_power_the_only_condition(lessons):
    # The two wordings that did: licence-free "inside set power limits" (demo) and "fine inside
    # its power limits" (explain), as if staying under a power limit were all Part 15 asks.
    lesson = lessons[5]
    for stage in lesson.stage_order:
        for phrase in ("inside set power limits", "inside its power limits"):
            assert phrase not in lesson.stages[stage], (
                f"M5 {stage} still reads as if power were the only Part 15 condition: {phrase!r}")
