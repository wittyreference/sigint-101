# ABOUTME: Tests the M8 "Two Shapes: AM vs FM" lesson pack loads as a valid six-stage intro —
# ABOUTME: id 8, every stage non-empty, and every predict choice a well-formed string key+label.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "08-am-vs-fm.yaml"


def test_m8_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 8
    assert lesson.slug == "am-vs-fm"
    assert lesson.title == "Two Shapes: AM vs FM"


def test_m8_has_all_six_intro_stages_non_empty():
    # An intro lesson teaches from a blank slate, so the console walks all six stages in order
    # (frame -> demo -> try -> predict -> observe -> explain); any one of them blank would strand
    # a beginner mid-lesson, which is the failure the loader exists to catch at load.
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m8_needs_a_radio_and_does_not_transmit():
    # M8 compares two live receptions on the waterfall, so it needs a real RTL-SDR attached; it is
    # receive-only (listen after you look), so it must never flag itself as a transmit lesson.
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m8_predict_choices_are_well_formed_string_key_label():
    # Guard against the past bug where choices used `value` instead of `key`: the console cannot
    # record a tap against a missing key. Every predict chip must carry a non-empty string key and
    # a non-empty string label, so a bool-like key (yes/no/on/off) quoted into a string also holds.
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M8 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
