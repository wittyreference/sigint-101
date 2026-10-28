# ABOUTME: Tests the M13 "The Sky Talks" ADS-B lesson pack loads as a valid six-stage intro —
# ABOUTME: id 13, every stage non-empty, string choices, the ADS-B radar instrument, the 1090 band.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "13-adsb.yaml"


def test_m13_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 13
    assert lesson.slug == "adsb"


def test_m13_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m13_needs_a_radio_and_does_not_transmit():
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m13_uses_the_adsb_instrument_not_the_waterfall():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_adsb is True
    assert lesson.panels == ["adsb"]


def test_m13_tunes_to_the_1090_mhz_band():
    lesson = load_lesson_file(PACK)
    assert lesson.tune is not None, "M13 must name the 1090 MHz ADS-B band"
    assert lesson.tune["center_hz"] == 1_090_000_000


def test_m13_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M13 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
