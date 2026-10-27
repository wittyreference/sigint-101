# ABOUTME: Tests the M9 "The Carrier That Vanishes" OOK lesson pack loads as a valid six-stage
# ABOUTME: intro — id 9, every stage non-empty, string key+label choices, tuned to the 433.92 band.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "09-on-off-keying.yaml"


def test_m9_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 9
    assert lesson.slug == "on-off-keying"


def test_m9_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m9_needs_a_radio_and_does_not_transmit():
    # M9 watches a 433 MHz OOK carrier blink on the waterfall, so it needs a real RTL-SDR; the
    # console only receives (the Flipper does any transmitting), so it must not flag requires_tx.
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m9_starts_the_instrument_on_the_433_ism_band():
    # The whole lesson is "watch the carrier vanish" at 433.92 MHz; a learner cannot hand-tune the
    # FM default across to it, so the pack must point the sweep at the ISM band itself.
    lesson = load_lesson_file(PACK)
    assert lesson.tune is not None, "M9 must declare its own tune band"
    assert lesson.tune["center_hz"] == 433_920_000
    assert lesson.tune["low_hz"] < 433_920_000 < lesson.tune["high_hz"]


def test_m9_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M9 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
