# ABOUTME: Tests the M18 "The Camera's Fingerprint" pack loads as a valid six-stage intro —
# ABOUTME: id 18, the flock instrument, no SDR tune, and the honesty answer offered at the gate.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs"
        / "18-flock-detector.yaml")


def test_m18_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 18
    assert lesson.slug == "flock-detector"


def test_m18_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m18_uses_the_flock_instrument_on_a_separate_radio_not_the_sdr():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_flock is True
    assert lesson.panels == ["flock"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    # The Flock detector reads wlan1, not the SDR tuner, so the pack names no SDR tune band.
    assert lesson.tune is None


def test_m18_offers_the_honest_tiered_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M18 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
    # An OUI prefix without the IE fingerprint is only a weak hint — that answer must be on offer.
    assert any(c["key"] == "weak-hint" for c in lesson.choices)


def test_m18_carries_the_counter_surveillance_honesty_law_in_the_reveal():
    """The "no alert never means no camera" law sits past the predict gate, in explain."""
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    assert "no alert never means no camera" in lesson.stages["explain"].lower()
