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


def _clauses(sentence):
    """A sentence's clauses: split at a comma, semicolon, colon, dash or bracket, "so" or "but"."""
    return [c.strip() for c in re.split(r"[,;:—–()]|\s(?:so|but)\s", sentence) if c.strip()]


# The +164 ppm in M4 was the reading of the one radio the course was built on. Every unit
# carries an error of its own, and the repo itself puts a generic board at 30-100 ppm
# (tests/test_ppm.py). So every ppm figure the learner reads must belong to something other than
# their own radio: the build unit ("built on", "ours"), the V3 or the pilot's rule ("V3", "2 Hz",
# "by rule"), or a supposition ("say", "suppose", "imagine", "if", "assume", "whether"). Two more
# checks catch what that rule cannot see: "hundred", a figure in words, and "expect a number" or
# "expect a reading", a figure given without "ppm". The try must point the learner at their own
# panel's number instead.
HUNDRED = re.compile(r"\bhundreds?\b", re.I)
EXPECT_FIGURE = re.compile(r"\bexpect\w*\s+(?:a|an|the|your)?\s*(?:number|reading|figure|value)\b",
                           re.I)
PPM_FIGURE = re.compile(r"\d[\d.,]*\s*ppm\b", re.I)
NOT_THE_LEARNERS = re.compile(r"\bbuilt on\b|\bours\b|\bV3\b|\b2 Hz\b|\bby rule\b", re.I)
POSED = re.compile(r"\b(?:say|suppose|imagine|if|assum\w*|whether)\b", re.I)


def test_m4_try_sends_the_learner_to_their_own_reading(unit1_lessons):
    lesson = unit1_lessons[4]
    given = [s for s in _learner_text(lesson)
             if HUNDRED.search(s) or EXPECT_FIGURE.search(s)
             or (PPM_FIGURE.search(s) and not (NOT_THE_LEARNERS.search(s) or POSED.search(s)))]
    assert not given, f"M4 hands the learner a figure for their own radio: {given}"
    own = [s for s in _sentences(lesson.stages["try"])
           if re.search(r"\b(number|reading|figure|value)\b", s, re.I)
           and re.search(r"\b(panel|shows?|reads?|your|yours|own)\b", s, re.I)]
    assert own, "M4 try must point the learner at the number their own panel shows"


# The explain once said "one cheap crystal sets every frequency". That holds only if the tuner
# and the sampler share one crystal, and nothing has confirmed that on the unit the learner
# holds: the osmocom rtl-sdr driver gives an R828D tuner, other than the RTL-SDR Blog V4, a
# 16 MHz crystal of its own beside the RTL2832U's 28.8 MHz (src/librtlsdr.c,
# include/tuner_r82xx.h). Until a check on the unit settles it, no line the learner reads may
# state it as fact: one, a single or the same crystal, clock, oscillator or reference; one that
# sets or clocks every frequency or both parts; or the tuner and the sampler on one. A hedge
# ("if", "may", "not yet confirmed") counts only inside the clause that makes the claim.
SOURCE = r"(?:crystal|clock|oscillator|reference)s?"
ONE_CRYSTAL = re.compile("|".join([
    rf"\b(?:one|single|same|shared?|shares)\b.*\b{SOURCE}\b",
    rf"\b{SOURCE}\b.*\b(?:sets?|clocks?|drives?|feeds?|times|runs?)\b.*\b(?:every|both|all)\b",
    rf"^(?=.*\b{SOURCE}\b)(?=.*\btuner\b)(?=.*\b(?:sampler|sample rate|sampling)\b)",
]), re.I)
HEDGE = re.compile(r"\b(?:if|whether|assum\w*|suppos\w*|may|might|unless|unconfirmed"
                   r"|not (?:yet )?(?:confirmed|checked|known))\b", re.I)


def test_m4_does_not_state_one_crystal_as_fact(unit1_lessons):
    claims = [c for s in _learner_text(unit1_lessons[4]) for c in _clauses(s)
              if ONE_CRYSTAL.search(c) and not HEDGE.search(c)]
    assert not claims, f"M4 states one crystal for every frequency as fact: {claims}"


# 47 CFR 73.322(a)(2) holds the stereo pilot to 19 kHz plus or minus 2 Hz: about 105 ppm, the
# same order as the error M4 measures. The panel takes the pilot as exactly 19 kHz
# (sigint101/ppm.py, PILOT_HZ), so even a perfect clock reads the pilot's own error.
EXACT = r"(?:honest|exact|accura|precise|true|perfect)"
TOLERANCE = re.compile(r"\b(?:2 Hz|two hertz|105 ppm|105 parts per million)\b", re.I)
NEAR_ZERO = re.compile(r"\b(?:near|nearly|close to|about|around|almost|roughly|read|reads"
                       r"|shows?|at|be|is)\s+(?:zero|0)\b", re.I)
PILOT_CONDITION = re.compile(
    rf"\bpilot\w*\b(?=.*\b(?:{EXACT}\w*|own (?:error|offset)|tolerance)\b)"
    rf"|\b{EXACT}\w*\b(?=.*\bpilot)", re.I)

# A sentence that calls the pilot, its tone or the landmark honest, exact, accurate, precise,
# true or perfect must say how far, naming the 2 Hz or the 105 ppm it comes to. A clause that
# says the pilot is, keeps or is held exact fails unless it opens a condition (if, unless,
# provided, whether), so "only the pilot is exact" fails. Any other clause passes only when
# "only", "if", "unless" or "provided" sets the exactness as a condition ("only against a pilot
# close to exact"), or when a promise of a reading near zero names an indefinite pilot ("against
# an exact pilot"). "The station's exact pilot" says that pilot is exact. An exactness word that
# is negated ("the pilot isn't exact") or that describes another thing ("a perfect clock") vouches
# for nothing.
REFERENCE = re.compile(r"\b(?:pilot|landmark|tone)s?\b", re.I)
VOUCH = re.compile(rf"\b{EXACT}\w*", re.I)
SAID_TO_BE = re.compile(r"\b(?:is|are|was|be|keeps?|kept|holds?|held|stays?|remains?)\b"
                        rf"(?:\s+[\w'-]+){{0,3}}?\s+{EXACT}", re.I)
OPENS_CONDITION = re.compile(r"\b(?:if|unless|provided|whether)\b", re.I)
CONDITION = re.compile(r"\b(?:only|if|unless|provided)\b(?=.*\b(?:pilot|landmark|tone)s?\b)"
                       rf"(?=.*\b{EXACT})", re.I)
INDEFINITE = re.compile(rf"\b(?:an?|any)\s+(?:\w+\s+)?{EXACT}\w*\s+pilot", re.I)
NEGATED = re.compile(r"(?:\b(?:not|never|no|nor|neither)\b|n't)"
                     rf"(?:\s+[\w'-]+){{0,3}}?\s+{EXACT}", re.I)
OTHER_THING = re.compile(rf"\b{EXACT}\w*\s+"
                         r"(?:clock|crystal|oscillator|radio|receiver|V3|dongle)s?\b", re.I)


def _vouches_without_tolerance(sentence):
    """Whether a sentence vouches for the pilot with neither its tolerance nor a condition."""
    if not (REFERENCE.search(sentence) and VOUCH.search(sentence)) or TOLERANCE.search(sentence):
        return False
    for clause in (c for c in _clauses(sentence) if VOUCH.search(c)):
        if NEGATED.search(clause) or OTHER_THING.search(clause):
            continue
        if SAID_TO_BE.search(clause):
            if OPENS_CONDITION.search(clause):
                continue
            return True
        if CONDITION.search(clause):
            continue
        if NEAR_ZERO.search(sentence) and INDEFINITE.search(clause):
            continue
        return True
    return False


def test_m4_names_the_pilots_tolerance_where_it_vouches_for_it(unit1_lessons):
    unqualified = [s for s in _learner_text(unit1_lessons[4]) if _vouches_without_tolerance(s)]
    assert not unqualified, f"M4 vouches for the pilot without its tolerance: {unqualified}"


# A line that promises a reading near zero must set the pilot's condition in the same sentence:
# a pilot close to exact, or the pilot's own error or tolerance. Another zero ("Zero-IF") is not
# a reading.
def test_m4_ties_a_near_zero_reading_to_the_pilot(unit1_lessons):
    untied = [s for s in _learner_text(unit1_lessons[4])
              if NEAR_ZERO.search(s) and not (PILOT_CONDITION.search(s) or TOLERANCE.search(s))]
    assert not untied, f"M4 promises a reading near zero without the pilot's condition: {untied}"
