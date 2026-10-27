# ABOUTME: Tests the M11 "Below the Noise Floor" LoRa lesson pack loads as a valid six-stage intro —
# ABOUTME: id 11, every stage non-empty, string choices, the LoRa (meshtastic) decode instrument.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "11-lora.yaml"


def test_m11_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 11
    assert lesson.slug == "lora"


def test_m11_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m11_needs_a_radio_and_does_not_transmit():
    # M11 reads packets the Nomad's LoRa radio received; node B does the transmitting, so the
    # console only receives and must not flag requires_tx.
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m11_uses_the_lora_instrument_not_the_waterfall():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_lora is True
    assert lesson.panels == ["lora"]


def test_m11_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M11 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
