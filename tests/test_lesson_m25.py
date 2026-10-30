# ABOUTME: Tests the M25 "A Picture in the Air" pack loads as a valid six-stage intro — id 25, the
# ABOUTME: sstv image instrument on 2m (145.8 MHz), and the real-photo answer at the predict gate.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "25-sstv.yaml")


def test_m25_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 25
    assert lesson.slug == "sstv"


def test_m25_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m25_uses_the_sstv_image_instrument_on_2m():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_sstv is True
    assert lesson.panels == ["sstv"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    assert lesson.tune is not None
    assert lesson.tune["center_hz"] == 145_800_000


def test_m25_offers_the_real_photo_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M25 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # SSTV is a picture written in sound, in the clear — a real photo appears.
    assert any(c["key"] == "photo" for c in lesson.choices)


def test_m25_explains_the_vis_header_and_tone_sweep_in_the_reveal():
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    reveal = lesson.stages["explain"].lower()
    assert "vis" in reveal
    assert "tone" in reveal
