# ABOUTME: Tests the M29 "Meet the Flipper" pack loads as a valid six-stage intro — id 29, the
# ABOUTME: flipper instrument on a separate USB device, and the field-tool-plus-bench thesis.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "29-meet-flipper.yaml")


def test_m29_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 29
    assert lesson.slug == "meet-flipper"


def test_m29_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m29_uses_the_flipper_instrument_on_a_separate_device_not_the_sdr():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_flipper is True
    assert lesson.panels == ["flipper"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    # The Flipper is read over USB serial, its own device, not the SDR tuner — no tune band.
    assert lesson.tune is None


def test_m29_offers_the_bench_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M29 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # Plugged into the nomad the Flipper gains a bench (screen, storage, lessons), not more radios.
    assert any(c["key"] == "a-bench" for c in lesson.choices)


def test_m29_carries_the_field_tool_plus_bench_thesis_in_the_reveal():
    """Standalone it is the field tool; plugged into the nomad it gains the analyst's bench."""
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    reveal = lesson.stages["explain"].lower()
    assert "bench" in reveal
    assert "field tool" in reveal
