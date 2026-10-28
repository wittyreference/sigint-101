# ABOUTME: Tests the M14 "The Return Address" APRS lesson pack loads as a valid six-stage intro —
# ABOUTME: id 14, every stage non-empty, string choices, the APRS decode instrument, the 2m band.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "14-aprs.yaml"


def test_m14_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 14
    assert lesson.slug == "aprs"


def test_m14_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m14_needs_a_radio_and_does_not_transmit():
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m14_uses_the_aprs_instrument_not_the_waterfall():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_aprs is True
    assert lesson.panels == ["aprs"]


def test_m14_tunes_to_the_2m_aprs_channel():
    lesson = load_lesson_file(PACK)
    assert lesson.tune is not None, "M14 must name the 2m APRS channel"
    assert lesson.tune["center_hz"] == 144_390_000


def test_m14_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M14 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
