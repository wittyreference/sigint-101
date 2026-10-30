# ABOUTME: Tests the M27 "The Coded Voice" pack loads as a valid six-stage intro — id 27, the dmr
# ABOUTME: instrument on the UHF digital-voice band, and the IDs-always/voice-sometimes answer.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "27-digital-voice.yaml")


def test_m27_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 27
    assert lesson.slug == "digital-voice"


def test_m27_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m27_uses_the_dmr_instrument_on_the_uhf_band():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_dmr is True
    assert lesson.panels == ["dmr"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    assert lesson.tune is not None
    assert lesson.tune["center_hz"] == 450_000_000


def test_m27_offers_the_ids_always_voice_sometimes_answer():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M27 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # The signaling is clear but the voice may be vocoded/encrypted: IDs always, speech sometimes.
    assert any(c["key"] == "ids-voice-sometimes" for c in lesson.choices)


def test_m27_explains_the_vocoder_and_encryption_in_the_reveal():
    """The explain stage (past the predict gate) names why a busy channel can decode no speech."""
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    reveal = lesson.stages["explain"].lower()
    assert "vocoder" in reveal
    assert "encrypt" in reveal
