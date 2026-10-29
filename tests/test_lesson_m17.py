# ABOUTME: Tests the M17 "Who's Watching" pack opens the counter-surveillance track as a valid
# ABOUTME: six-stage intro — no RF, the honesty answer offered, explain sealed until commit.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs"
        / "17-counter-surveillance-framing.yaml")


def test_m17_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 17
    assert lesson.slug == "watch-the-watchers"


def test_m17_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m17_is_a_no_rf_framing_lesson():
    """The track opener teaches the idea; it needs no radio and no instrument panel."""
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is False
    assert lesson.requires_tx is False
    assert lesson.panels == []
    assert lesson.tune is None


def test_m17_offers_the_honesty_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M17 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
    # The lesson's whole point: a silent detector proves nothing — that answer must be on offer.
    assert any(c["key"] == "proves-nothing" for c in lesson.choices)


def test_m17_seals_the_explain_reveal_until_a_prediction_is_committed():
    """The honesty payoff lives in explain, which sits past the predict gate."""
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    assert "frame" not in lesson.sealed_stages
