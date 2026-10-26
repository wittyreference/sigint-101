# ABOUTME: Tests the M1 "See the Invisible" lesson pack — the first shipped curriculum file —
# ABOUTME: loads as a valid six-stage intro lesson that needs a real radio on the bench.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "01-see-the-invisible.yaml"


def test_m1_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 1
    assert lesson.slug == "see-the-invisible"
    assert lesson.title == "See the Invisible"


def test_m1_has_all_six_intro_stages_non_empty():
    # An intro lesson teaches from a blank slate, so the console walks all six stages in
    # order (frame -> demo -> try -> predict -> observe -> explain); any one of them blank
    # would strand a true beginner mid-lesson, which is the one failure the loader exists to
    # catch at load rather than in front of the learner.
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m1_needs_a_radio_and_does_not_transmit():
    # M1 is the first thing a learner points at, so it needs a real RTL-SDR attached; it is
    # receive-only (witness-first), so it must never flag itself as a transmit lesson.
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
