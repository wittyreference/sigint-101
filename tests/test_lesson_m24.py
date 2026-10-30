# ABOUTME: Tests the M24 "A Picture From Space" pack loads as a valid six-stage intro — id 24, the
# ABOUTME: apt image instrument on the 137 MHz band, and the grainy-but-real answer at predict.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "24-noaa-apt.yaml")


def test_m24_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 24
    assert lesson.slug == "noaa-apt"


def test_m24_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m24_uses_the_apt_image_instrument_on_the_137_mhz_band():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_apt is True
    assert lesson.panels == ["apt"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    assert lesson.tune is not None
    assert lesson.tune["center_hz"] == 137_500_000


def test_m24_offers_the_grainy_but_real_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M24 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # APT is analog, so it degrades gracefully into a grainy-but-real picture, not all-or-nothing.
    assert any(c["key"] == "grainy" for c in lesson.choices)


def test_m24_explains_the_graceful_analog_degradation_in_the_reveal():
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    assert "analog" in lesson.stages["explain"].lower()
