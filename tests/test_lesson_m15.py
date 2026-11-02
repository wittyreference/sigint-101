# ABOUTME: Tests the M15 "Not Meant for You" POCSAG lesson pack loads as a valid six-stage intro —
# ABOUTME: id 15, every stage non-empty, string choices, the POCSAG instrument, the pager band.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file
from sigint101.pocsag import DEFAULT_POCSAG_HZ

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "15-pocsag.yaml"


def test_m15_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 15
    assert lesson.slug == "pocsag"


def test_m15_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m15_needs_a_radio_and_does_not_transmit():
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m15_uses_the_pocsag_instrument_not_the_waterfall():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_pocsag is True
    assert lesson.panels == ["pocsag"]


def test_m15_tunes_to_the_pager_band():
    lesson = load_lesson_file(PACK)
    assert lesson.tune is not None, "M15 must name the pager band"
    assert 928_000_000 <= lesson.tune["center_hz"] <= 932_000_000


def test_m15_teaches_the_statute_that_keeps_the_words_out_of_reach():
    # MC's call (2026-11-02): framing live, text hidden, and teach the statute. Audit X1: the old
    # copy called receiving pages "legal almost everywhere", which on a carrier's band is false.
    lesson = load_lesson_file(PACK)
    explain = lesson.stages["explain"]
    for cite in ("2510(16)(D)", "2510(12)(B)", "2511(1)(a)"):
        assert cite in explain, f"M15 explain must cite 18 USC {cite}"
    for stage in lesson.stage_order:
        assert "legal almost everywhere" not in lesson.stages[stage]
    assert lesson.answer == ("law",)


def test_m15_dial_reads_the_channel_the_decoder_listens_on():
    # The dial shows the pack's band; the decoder runs DEFAULT_POCSAG_HZ. They must agree.
    assert load_lesson_file(PACK).tune["center_hz"] == DEFAULT_POCSAG_HZ


def test_m15_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M15 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
