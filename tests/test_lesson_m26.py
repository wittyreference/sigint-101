# ABOUTME: Tests the M26 "The Sea Talks" pack loads as a valid six-stage intro — id 26, the ais
# ABOUTME: instrument on the marine VHF tune band, and the MMSI answer offered at the predict gate.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "26-ais.yaml")


def test_m26_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 26
    assert lesson.slug == "ais"


def test_m26_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m26_uses_the_ais_instrument_on_the_marine_vhf_band():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_ais is True
    assert lesson.panels == ["ais"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    # rtl_ais listens around the marine AIS pair (161.975 / 162.025 MHz).
    assert lesson.tune is not None
    assert lesson.tune["center_hz"] == 162_000_000


def test_m26_offers_the_shared_mmsi_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M26 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # A vessel's position and name ride in different messages; the shared MMSI stitches them.
    assert any(c["key"] == "mmsi" for c in lesson.choices)


def test_m26_names_the_mmsi_keying_in_the_reveal():
    """The explain stage (past the predict gate) names the MMSI as the roster key."""
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    assert "mmsi" in lesson.stages["explain"].lower()
