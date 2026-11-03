# ABOUTME: Tier-A item lint over every shipped predict (item review 11-03 §1.4): one answer key, a
# ABOUTME: short stem, chips of even length, and no key words given away before the commit.

import re
from pathlib import Path

import pytest
import yaml

from sigint101.lessons import parse_lesson

PACK_DIR = Path(__file__).resolve().parent.parent / "lesson_packs"
PACKS = {path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
         for path in sorted(PACK_DIR.glob("*.yaml"))}

# The rules, with the item-writing guideline each one stands on (Haladyna, Downing & Rodriguez
# 2002, "HDR" below; item review 11-03 §1.1 and §1.4).
STEM_MAX_WORDS = 45            # a stem read in one glance at 800x480; HDR 14-15, a clear stem
CHIP_LENGTH_RATIO = 2          # longest chip at most twice the shortest; HDR 28, long-answer cue
GIVEAWAY_STAGES = ("frame", "demo", "try")   # the screens a learner reads before the predict

# Words that carry no content, so a key sharing them with the screens gives nothing away. Yes/no
# and the quantifiers are here because they are the whole label of a yes/no chip.
STOPWORDS = frozenset("""
a about all also an and any are as at be been being but by can could d did do does down for from
he her here his how i if in into is it its just ll may me might more most must my no none nor not
of off on one only or our out over re s she should so some than that the their then there these
they this those to too up ve very was we were what when where which who why will with would yes
you your
""".split())

# A decimal stays one token ("2.4"), so a number in a key matches the same number on a screen.
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def content_words(text: str) -> set:
    """The words of a text that carry meaning: lowercased, punctuation dropped, stopwords out."""
    return {word for word in _TOKEN.findall(text.lower()) if word not in STOPWORDS}


def lint_pack(raw: dict) -> list:
    """Every (rule, detail) the predict of one raw pack trips; empty when it is clean.

    The raw YAML is read for `multi_key_reason`, which the loader does not carry; everything else
    comes off the loaded Lesson, so the lint sees exactly what the console renders.
    """
    lesson = parse_lesson(raw)
    findings = []

    if len(lesson.answer) != 1 and not str(raw.get("multi_key_reason") or "").strip():
        findings.append(("single-key", f"answer {list(lesson.answer)} names "
                                       f"{len(lesson.answer)} keys and no multi_key_reason"))

    stem_words = len(lesson.stages["predict"].split())
    if stem_words > STEM_MAX_WORDS:
        findings.append(("stem-length", f"predict is {stem_words} words; "
                                        f"keep it to {STEM_MAX_WORDS}"))

    labels = [choice["label"] for choice in lesson.choices]
    if labels and max(map(len, labels)) > CHIP_LENGTH_RATIO * min(map(len, labels)):
        findings.append(("chip-length", f"chip lengths {[len(label) for label in labels]} "
                                        f"spread past {CHIP_LENGTH_RATIO}x: {labels}"))

    shown = set().union(*(content_words(lesson.stages[stage])
                          for stage in GIVEAWAY_STAGES if stage in lesson.stages))
    by_key = {choice["key"]: choice["label"] for choice in lesson.choices}
    for key in lesson.answer:
        leaked = sorted(content_words(by_key[key]) & shown)
        if leaked:
            findings.append(("giveaway", f"key chip '{by_key[key]}' shares {leaked} with "
                                         f"{'/'.join(GIVEAWAY_STAGES)}"))
    return findings


def _rules(findings) -> list:
    return [rule for rule, _ in findings]


# --- each rule, proved on a deliberately bad pack ---------------------------------------------
# A clean pack, then one defect at a time: the lint must name exactly the rule that defect breaks.

def _pack(**overrides) -> dict:
    data = {
        "id": 90,
        "slug": "lint-fixture",
        "title": "Lint Fixture",
        "type": "intro",
        "concept": "a fixture for the item lint",
        "stages": {
            "frame": "Tonight you meet the gain knob.",
            "demo": "Watch the meter while the knob turns.",
            "try": "Step the knob from low to middle.",
            "predict": "From middle to maximum, what does the noise floor do? Lock it in.",
            "observe": "Watch the floor at maximum.",
            "explain": "It rises with the peak.",
        },
        "choices": [
            {"key": "rises", "label": "Rises with it"},
            {"key": "holds", "label": "Holds still"},
            {"key": "sinks", "label": "Sinks lower"},
        ],
        "answer": ["rises"],
        "verdict": "Rises: gain lifts signal and noise together.",
    }
    data.update(overrides)
    return data


def _with_stage(stage: str, text: str) -> dict:
    pack = _pack()
    pack["stages"] = {**pack["stages"], stage: text}
    return pack


def test_a_clean_pack_trips_nothing():
    assert lint_pack(_pack()) == []


def test_two_keys_with_no_written_reason_trip_single_key():
    assert _rules(lint_pack(_pack(answer=["rises", "holds"]))) == ["single-key"]


def test_two_keys_with_a_multi_key_reason_pass():
    pack = _pack(answer=["rises", "holds"],
                 multi_key_reason="The explain defends both: the floor holds, then rises.")
    assert lint_pack(pack) == []


def test_a_stem_over_45_words_trips_stem_length():
    stem = " ".join(["word"] * (STEM_MAX_WORDS + 1))
    assert _rules(lint_pack(_with_stage("predict", stem))) == ["stem-length"]


def test_a_stem_of_exactly_45_words_passes():
    assert lint_pack(_with_stage("predict", " ".join(["word"] * STEM_MAX_WORDS))) == []


def test_a_chip_more_than_twice_another_trips_chip_length():
    chips = [{"key": "rises", "label": "Rises"},
             {"key": "holds", "label": "Holds steady at the floor"},
             {"key": "sinks", "label": "Sinks"}]
    assert _rules(lint_pack(_pack(choices=chips))) == ["chip-length"]


def test_chips_at_exactly_twice_pass():
    chips = [{"key": "rises", "label": "Rises"},          # 5 characters
             {"key": "holds", "label": "Holds firm"},     # 10 characters: exactly twice
             {"key": "sinks", "label": "Sinks"}]
    assert lint_pack(_pack(choices=chips)) == []


def test_a_key_word_shown_in_the_demo_trips_giveaway():
    findings = lint_pack(_with_stage("demo", "Watch the floor RISE as the knob turns; it rises."))
    assert _rules(findings) == ["giveaway"]
    assert "rises" in findings[0][1]


@pytest.mark.parametrize("stage", GIVEAWAY_STAGES)
def test_every_screen_before_the_predict_is_searched(stage):
    assert _rules(lint_pack(_with_stage(stage, "The floor rises."))) == ["giveaway"]


def test_a_distractor_word_shown_early_is_not_a_giveaway():
    # Only the key's words cue the answer; a lure the screens mention is just a plausible lure.
    assert lint_pack(_with_stage("demo", "Some floors hold, some sink.")) == []


def test_a_number_in_the_key_matches_the_same_number_on_a_screen():
    chips = [{"key": "rises", "label": "2.4 MHz"},
             {"key": "holds", "label": "1.2 MHz"},
             {"key": "sinks", "label": "4.8 MHz"}]
    pack = _pack(choices=chips)
    pack["stages"] = {**pack["stages"], "try": "The dongle takes 2.4 million pairs a second."}
    findings = lint_pack(pack)
    assert _rules(findings) == ["giveaway"]
    assert "'2.4'" in findings[0][1]


def test_stopwords_in_the_key_never_count_as_a_giveaway():
    chips = [{"key": "rises", "label": "It is up"},
             {"key": "holds", "label": "Holds"},
             {"key": "sinks", "label": "Sinks"}]
    pack = _pack(choices=chips)
    pack["stages"] = {**pack["stages"], "demo": "It is what it is, all the way up."}
    assert lint_pack(pack) == []


# --- the shipped packs ------------------------------------------------------------------------
# A pack that trips a rule today is waived here, one (lesson id, rule) at a time, each with the
# item review's own flag for that item as its reason (the review's L numbers are lesson ids). A
# waiver is a debt, not a pass: the next test fails the moment one is no longer needed, so the
# table only ever shrinks.

_REWRITE = "BROKEN in the 11-03 item review: being rewritten in a parallel PR"

WAIVERS = {
    (2, "single-key"): _REWRITE,
    (2, "stem-length"): _REWRITE,
    (2, "chip-length"): _REWRITE,
    (3, "stem-length"): _REWRITE,
    (3, "chip-length"): _REWRITE,
    (7, "single-key"): _REWRITE,
    (7, "chip-length"): _REWRITE,
    (8, "chip-length"): _REWRITE,
    (4, "chip-length"): "OK in the review: the long chip is the 'About the same' lure, not the key",
    (5, "chip-length"): "MINOR (R+) in the review: the long chip is the 'if you identify' lure",
    (9, "giveaway"): "MINOR (T, R+, H) in the review: the demo already says the carrier goes",
    (10, "giveaway"): "MINOR (R+, D) in the review: the screens name 'frequency-shift'",
    (13, "chip-length"): "OK in the review, but the key is the longest chip: fix at next reword",
    (14, "giveaway"): "MINOR (S, D, R+) in the review: frame and demo list callsign and path",
    (16, "giveaway"): "MINOR (S, K) in the review: the try says 'some readable text'",
    (17, "chip-length"): "OK in the review: Yes/No/Mostly, and the key 'No' is the shortest chip",
    (19, "chip-length"): "OK in the review: Yes/No/Nearly, and the key 'No' is the shortest chip",
    (20, "giveaway"): "MINOR (R+, D) in the review: the demo says the first stage sets sensitivity",
    (22, "giveaway"): "OK in the review: the 2.4 MSPS rate must be shown to pose the question",
    (24, "giveaway"): "MINOR (K, H) in the review: 'picture' is the lesson's subject, not a cue",
    (26, "giveaway"): "MINOR (D) in the review, which notes the demo and try cue the shared ID",
    (29, "giveaway"): "OK in the review: 'screen' only describes the device in frame and demo",
}


def _unwaived(name: str) -> list:
    raw = PACKS[name]
    return [(rule, detail) for rule, detail in lint_pack(raw)
            if (raw["id"], rule) not in WAIVERS]


@pytest.mark.parametrize("name", sorted(PACKS))
def test_every_shipped_predict_passes_the_lint(name):
    assert _unwaived(name) == []


def test_every_waiver_is_still_needed():
    tripped = {(raw["id"], rule) for raw in PACKS.values() for rule, _ in lint_pack(raw)}
    assert sorted(set(WAIVERS) - tripped) == []
