# ABOUTME: Tests the M17 "Who's Watching" pack opens the counter-surveillance track as a valid
# ABOUTME: six-stage intro — no RF, the honesty answer offered, explain sealed until commit.

import re
from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs"
        / "17-counter-surveillance-framing.yaml")

# The explain closed on what comes next: "a real Flock-camera detector on your own radio, then
# catching fake towers with a borrowed one — both receive-only." The borrowed one is an Orbic
# running EFF's Rayhunter, a cellular hotspot that talks to the tower it is attached to, read by
# the console over WiFi, which transmits too. So no set-up that takes in the fake-tower detector is
# receive-only. A receive-only label (receive-only, RX-only, listen-only, only or strictly
# receives, one-way) is read as applying to the nearest thing its sentence names before it. The
# learner's own radio (the Flock detector, the SDR, the dongle) passes. The fake-tower side (a
# tower, catcher, Stingray, IMSI, Orbic, Rayhunter, hotspot, modem, cell, the borrowed one), a
# whole (both, all, each, every, the track) or nothing named at all fails.
RX_ONLY = re.compile(r"\breceiv(?:e|es|ing)[\s-]+only\b|\brx[\s-]*only\b|\blisten-only\b"
                     r"|\b(?:only|just|merely|strictly|purely)\s+receiv\w*|\bone[\s-]way\b", re.I)
SUBJECT = re.compile(
    r"(?P<own>\bflock\w*|\bcamera[- ](?:detector|catcher)s?\b|\bown\s+radio\b|\bwlan\d*\b"
    r"|\bsdr\b|\bdongle\b)"
    r"|\b(?:towers?|catchers?|stingrays?|imsi|orbic|rayhunter|hotspot|modem|cell\S*|borrowed"
    r"|both|all|each|every|whole|track)\b", re.I)


def _calls_the_fake_tower_set_up_receive_only(sentence):
    """True when a receive-only label applies to anything but the learner's own radio."""
    for label in RX_ONLY.finditer(sentence):
        named = list(SUBJECT.finditer(sentence, 0, label.start()))
        if not named or not named[-1].group("own"):
            return True
    return False


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


def test_m17_never_calls_the_fake_tower_set_up_receive_only():
    lesson = load_lesson_file(PACK)
    texts = [lesson.concept, *lesson.stages.values(), lesson.verdict,
             *(choice["label"] for choice in lesson.choices)]
    if lesson.check:
        texts.append(lesson.check["prompt"])
        texts.extend(choice["why"] for choice in lesson.check["choices"])
    sentences = [s for text in texts for s in re.split(r"(?<=[.?!])\s+", text) if s]
    claims = [s for s in sentences if _calls_the_fake_tower_set_up_receive_only(s)]
    assert not claims, f"M17 calls a set-up with the borrowed Orbic in it receive-only: {claims}"
