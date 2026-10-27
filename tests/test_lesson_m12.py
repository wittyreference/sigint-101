# ABOUTME: Tests the M12 "The Hidden Text" RDS/PSK lesson pack loads as a valid six-stage intro —
# ABOUTME: id 12, every stage non-empty, string choices, the RDS decode instrument, an FM-band tune.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "12-rds.yaml"


def test_m12_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 12
    assert lesson.slug == "rds"


def test_m12_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m12_needs_a_radio_and_does_not_transmit():
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m12_uses_the_rds_instrument_not_the_waterfall():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_rds is True
    assert lesson.panels == ["rds"]


def test_m12_tunes_to_an_fm_station():
    lesson = load_lesson_file(PACK)
    assert lesson.tune is not None, "M12 must point redsea at an FM station"
    assert 88_000_000 <= lesson.tune["center_hz"] <= 108_000_000


def test_m12_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M12 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
