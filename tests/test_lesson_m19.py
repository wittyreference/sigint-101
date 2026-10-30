# ABOUTME: Tests the M19 "Catch the Catcher" pack loads as a valid six-stage intro — id 19, the
# ABOUTME: rayhunter instrument on a separate device, and the look-closer honesty at predict.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "19-catch-the-catcher.yaml")


def test_m19_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 19
    assert lesson.slug == "catch-the-catcher"


def test_m19_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m19_uses_the_rayhunter_instrument_on_a_separate_device_not_the_sdr():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_rayhunter is True
    assert lesson.panels == ["rayhunter"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    # The Orbic running Rayhunter is its own device over WiFi, not the SDR tuner — no tune band.
    assert lesson.tune is None


def test_m19_offers_the_look_closer_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M19 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # A red flag is a heuristic, not proof — red means look closer, never a verdict.
    assert any(c["key"] == "look-closer" for c in lesson.choices)


def test_m19_carries_the_counter_surveillance_honesty_in_the_reveal():
    """Red is not proof and a green line never proves absence — the law past the predict gate."""
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    reveal = lesson.stages["explain"].lower()
    assert "look closer" in reveal
    assert "never means no" in reveal
