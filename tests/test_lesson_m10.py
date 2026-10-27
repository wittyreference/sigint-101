# ABOUTME: Tests the M10 "The Carrier That Shifts" FSK lesson pack loads as a valid six-stage
# ABOUTME: intro — id 10, every stage non-empty, string choices, the decode instrument, ISM band.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "10-frequency-shift-keying.yaml"


def test_m10_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 10
    assert lesson.slug == "frequency-shift-keying"


def test_m10_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m10_needs_a_radio_and_does_not_transmit():
    # M10 reads an FSK sensor on the ISM band with rtl_433, so it needs a real RTL-SDR; the console
    # only receives, so it must not flag requires_tx.
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m10_uses_the_decoder_not_the_waterfall():
    # FSK's two tones do not resolve on a 125 kHz-bin waterfall, so M10's instrument is the decoder.
    lesson = load_lesson_file(PACK)
    assert lesson.uses_decoder is True
    assert lesson.panels == ["decode"]


def test_m10_starts_on_the_433_ism_band():
    lesson = load_lesson_file(PACK)
    assert lesson.tune is not None, "M10 must declare its own ISM tune band"
    assert lesson.tune["center_hz"] == 433_920_000


def test_m10_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M10 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
