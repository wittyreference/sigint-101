# ABOUTME: Tests the Unit 1 lesson packs (M1-M4) load together via load_lessons: all four
# ABOUTME: present, each a six-stage intro with its declared panel and well-formed predict choices.

import re
from pathlib import Path

import pytest

from sigint101.lessons import REQUIRED_STAGES, load_lessons

# load_lessons takes a path relative to the working directory. The acceptance gate
# (make selftest) runs pytest from the repo root, but pin the cwd here so the test is correct
# no matter where pytest is invoked, while still calling the loader exactly as the console does.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The four Unit 1 packs and the extra panel each one turns on beyond the waterfall baseline:
# M1 needs no extra panel; M2 the gain panel (SNR), M3 provenance (baseline), M4 ppm (error).
EXPECTED_PANELS = {
    1: [],
    2: ["gain"],
    3: ["provenance"],
    4: ["ppm"],
}


@pytest.fixture
def unit1_lessons(monkeypatch):
    """Load every pack in lesson_packs/ the way the console does, from the repo root."""
    monkeypatch.chdir(REPO_ROOT)
    return load_lessons("lesson_packs")


def test_unit1_has_all_four_lessons(unit1_lessons):
    # Unit 1 is M1-M4; a missing id means a pack failed to load or was never written.
    for lesson_id in (1, 2, 3, 4):
        assert lesson_id in unit1_lessons, f"lesson {lesson_id} is missing from lesson_packs/"


def test_each_unit1_lesson_is_a_six_stage_intro(unit1_lessons):
    # Every Unit 1 pack teaches a concept from a blank slate, so it is an intro lesson and the
    # console walks all six stages in order; any blank stage would strand a beginner mid-lesson.
    # Unit 1 pilots the post-lesson check, which walks last, after the six.
    for lesson_id in (1, 2, 3, 4):
        lesson = unit1_lessons[lesson_id]
        assert lesson.type == "intro", f"lesson {lesson_id} must be an intro lesson"
        assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain",
                                      "check")
        for stage in REQUIRED_STAGES["intro"]:
            assert lesson.stages[stage].strip(), (
                f"lesson {lesson_id} stage {stage!r} must carry real teaching text"
            )


def test_each_unit1_lesson_declares_its_panel(unit1_lessons):
    # Each pack turns on exactly the console panel its concept needs: gain for SNR (M2),
    # provenance for the baseline (M3), ppm for frequency error (M4); M1 needs none.
    for lesson_id, panels in EXPECTED_PANELS.items():
        assert unit1_lessons[lesson_id].panels == panels


def test_every_pack_choice_has_a_key_and_a_label(unit1_lessons):
    # Guard against the past bug: predict choices once used `value` instead of `key`, which the
    # console's loader cannot record a tap against. Every pack carries predict choices, and every
    # entry must have both a non-empty key and a non-empty label.
    for lesson_id, lesson in unit1_lessons.items():
        assert lesson.choices, f"lesson {lesson_id} must offer predict choices"
        for choice in lesson.choices:
            assert choice.get("key"), f"lesson {lesson_id} has a choice missing 'key'"
            assert choice.get("label"), f"lesson {lesson_id} has a choice missing 'label'"


def _sentences(text):
    """A stage's sentences: split after a full stop, question mark or bang and a space."""
    return [s for s in re.split(r"(?<=[.?!])\s+", text) if s]


def _learner_text(lesson):
    """Every sentence the learner reads in a lesson: concept, stages, verdict and check."""
    texts = [lesson.concept, *lesson.stages.values(), lesson.verdict]
    if lesson.check:
        texts.append(lesson.check["prompt"])
        texts.extend(choice["why"] for choice in lesson.check["choices"])
    return [s for text in texts for s in _sentences(text)]


def test_m4_try_sends_the_learner_to_their_own_reading(unit1_lessons):
    # The +164 ppm in M4 was the reading of the one radio the course was built on; every unit
    # carries an error of its own. The try once told every learner to "expect a number in the
    # low hundreds". It must point them at the number their own panel measures instead. Any
    # sentence that names the number together with the panel, or with the learner's own, will do.
    text = unit1_lessons[4].stages["try"]
    assert "low hundreds" not in text.lower(), "M4 try still gives a range to expect"
    assert "expect a number" not in text.lower(), "M4 try still tells the learner what to expect"
    own = [s for s in _sentences(text)
           if re.search(r"\b(number|reading|figure|value)\b", s, re.I)
           and re.search(r"\b(panel|shows?|reads?|your|yours|own)\b", s, re.I)]
    assert own, "M4 try must point the learner at the number their own panel shows"


# The explain once said "one cheap crystal sets every frequency". That holds only if the tuner
# and the sampler share one crystal, and nothing has confirmed that on the unit the learner
# holds: the osmocom rtl-sdr driver gives an R828D tuner, other than the RTL-SDR Blog V4, a
# 16 MHz crystal of its own beside the RTL2832U's 28.8 MHz (src/librtlsdr.c,
# include/tuner_r82xx.h). Until a check on the unit settles it, no line the learner reads may
# state it as fact. A hedged mention ("if", "may", "not yet confirmed") stays allowed.
SINGLE_CRYSTAL = re.compile(r"\b(one|a single|single|same)\b[^.;:]*\b(crystal|clock)", re.I)
HEDGE = re.compile(r"\b(if|whether|assum\w*|may|might|unless|unconfirmed"
                   r"|not (yet )?(confirmed|checked))\b", re.I)


def test_m4_does_not_state_one_crystal_as_fact(unit1_lessons):
    claims = [s for s in _learner_text(unit1_lessons[4])
              if SINGLE_CRYSTAL.search(s) and not HEDGE.search(s)]
    assert not claims, f"M4 states one crystal for every frequency as fact: {claims}"


# 47 CFR 73.322(a)(2) holds the stereo pilot to 19 kHz plus or minus 2 Hz: about 105 ppm, the
# same order as the error M4 measures. Where the lesson vouches for the pilot as honest, it must
# say how far, naming the 2 Hz or the ppm it comes to.
def test_m4_names_the_pilots_tolerance_where_it_vouches_for_it(unit1_lessons):
    vouching = [s for s in _learner_text(unit1_lessons[4]) if re.search(r"\bhonest", s, re.I)]
    unqualified = [s for s in vouching if not re.search(r"\b2 Hz\b|\b105 ppm\b", s)]
    assert not unqualified, f"M4 vouches for the pilot without its tolerance: {unqualified}"


# The panel takes the pilot as exactly 19 kHz (sigint101/ppm.py, PILOT_HZ), so even a perfect
# clock reads the pilot's own error, which the rule lets reach about 105 ppm. A radio that
# "would read near zero" does so only against a pilot close to exact: the sentence that promises
# it must name the pilot.
def test_m4_ties_a_near_zero_reading_to_the_pilot(unit1_lessons):
    zero = [s for s in _sentences(unit1_lessons[4].stages["explain"])
            if re.search(r"\bzero\b", s, re.I)]
    untied = [s for s in zero if "pilot" not in s.lower()]
    assert not untied, f"M4 explain promises near zero with no word on the pilot: {untied}"
