# ABOUTME: Tests the M16 "What the Aircraft Says" ACARS pack loads as a valid six-stage intro —
# ABOUTME: id 16, every stage non-empty, string choices, the ACARS instrument, the VHF channel.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "16-acars.yaml"


def test_m16_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 16
    assert lesson.slug == "acars"


def test_m16_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m16_needs_a_radio_and_does_not_transmit():
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m16_uses_the_acars_instrument_not_the_waterfall():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_acars is True
    assert lesson.panels == ["acars"]


def test_m16_tunes_to_the_vhf_acars_channel():
    lesson = load_lesson_file(PACK)
    assert lesson.tune is not None, "M16 must name the VHF ACARS channel"
    assert lesson.tune["center_hz"] == 131_550_000


def test_m16_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M16 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
