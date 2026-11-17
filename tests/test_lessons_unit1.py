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


# M4 once taught that the pilot's figure is the dial's error and that `-p <figure>` cures it. On
# the build unit that is false: its radio has two clocks. Measured on the kiosk on 2026-11-17,
# receive only, the pilot gave +144 ppm, the error of the RTL2832U's clock, which times the
# samples; the dial, an R828D tuner on a reference of its own, read high by a smaller, different
# amount, and `-p 144` moved every carrier further from its channel. The osmocom driver gives one
# `-p` figure to both the sample rate and the tuner's crystal value (src/librtlsdr.c,
# rtlsdr_set_freq_correction and rtlsdr_get_xtal_freq). So M4 keeps the measurement, says what it
# measures, gives the dial an error of its own, and drops the cure and every distance worked from
# the figure. Each test below reads every line the learner sees: the sentences, the chip labels,
# and the check read whole where its parts answer each other.
def _learner_lines(lesson):
    """Every line the learner reads in a lesson: its sentences, then every chip label."""
    labels = [choice["label"] for choice in lesson.choices]
    if lesson.check:
        labels += [choice["label"] for choice in lesson.check["choices"]]
    return _learner_text(lesson) + labels


# The pilot's own numbers and the V3's rating are not the figure the panel reads: the 19 kHz tone,
# the 2 Hz and 105 ppm the rule holds it to, and the V3's 1 ppm. A line is read with them blanked.
CONSTANTS = re.compile(r"\b(?:19 kHz|2 Hz|105 ppm|1 ppm)\b", re.I)

# The pilot's figure as a line names it: a reading in ppm or parts per million ("+164 ppm"), a
# signed figure ("+164"), the pilot itself, or a number, figure, reading or ppm that is the panel's,
# the learner's or the measured one ("the number your panel shows", "your measured ppm").
READING = re.compile(r"[+−-]?\d[\d.,]*\s*(?:ppm|parts per million)\b|(?<![\w.])[+−]\d", re.I)
FIGURE = re.compile(r"\bpilot'?s?\b|\b(?:measured|panel'?s|your|that|this|the)\s+(?:own\s+)?"
                    r"(?:number|figure|reading|ppm)\b", re.I)

# The dial's error as a line names it: the dial or the tuner, what you tune, every, any or each
# MHz, frequency, band, station or channel, everywhere or wherever, the radio's frequency error,
# how far off, or landing high, low or off. The dial the learner steps or turns is the control,
# not its error ("Step the dial to your strongest station"), and so is "Tune ..." said to them.
DIAL = re.compile(r"(?<!step the )(?<!turn the )\b(?:dial|tuner)'?s?\b|\byou(?:'ve| have)? tuned?\b"
                  r"|\b(?:every|any|each) (?:MHz|frequency|band|station|channel)\b"
                  r"|\b(?:everywhere|wherever)\b|\bfrequency error\b|\bhow far off\b"
                  r"|\blands?\b[^.;]*\b(?:high|low|off)\b", re.I)

# A line sets the two apart when it negates the dial ("not the dial's", "nothing about the
# tuner") or gives the dial an error of its own or a separate one ("the dial has its own").
APART = re.compile(r"(?:\b(?:not|never|nothing|no)\b|n't)(?:\W+\w+){0,3}?\W+(?:dial|tuner)"
                   r"|\b(?:dial|tuner)\b[^.;]*\b(?:own|separate\w*)\b"
                   r"|\bseparate\w*\b[^.;]*\b(?:dial|tuner)\b", re.I)

# Hertz for every MHz tuned makes a figure the dial's error whatever it is tied to ("164 Hz of
# error for every MHz you tune"), and either way round.
HZ_PER_MHZ = re.compile(r"\d[\d,.]*\s*Hz\b[^.;]*\b(?:per|every|each)\s+MHz\b"
                        r"|\b(?:per|every|each)\s+MHz\b[^.;]*\d[\d,.]*\s*Hz\b", re.I)


def _makes_the_figure_the_dials_error(line):
    """Whether a line ties the pilot's figure to the dial's error and does not set them apart."""
    line = CONSTANTS.sub(" ", line)
    if HZ_PER_MHZ.search(line):
        return True
    return bool((READING.search(line) or FIGURE.search(line)) and DIAL.search(line)
                and not APART.search(line))


# The check's prompt is also read whole: "Say your radio reads +164 ppm. ... About how far off
# frequency does it land?" ties the two across its sentences.
def test_m4_never_makes_the_pilots_figure_the_dials_error(unit1_lessons):
    lesson = unit1_lessons[4]
    prompt = [lesson.check["prompt"]] if lesson.check else []
    tied = [line for line in _learner_lines(lesson) + prompt
            if _makes_the_figure_the_dials_error(line)]
    assert not tied, f"M4 makes the pilot's figure the dial's error: {tied}"


# The cure: `-p` or `--ppm` given a value ("-p 164", "--ppm <yours>"), or named beside the figure
# or a word for curing ("the cure is -p"), or tools told, given or passed something ("tell each
# tool your measured number").
OPTION = r"(?<![\w-])(?:-p|--ppm)(?![\w-])"
CURING = r"\b(?:cure|correct|fix|apply|set|tell|give|pass|feed|hand)\w*"
CURE = re.compile(rf"{OPTION}\s*(?:[+−-]?\d|<)|{OPTION}.*{CURING}|{CURING}.*{OPTION}"
                  rf"|{CURING}[^.?!]*\btools?\b|\btools?\b[^.?!]*{CURING}", re.I)


def test_m4_gives_no_cure_from_the_pilots_figure(unit1_lessons):
    cures = [line for line in _learner_lines(unit1_lessons[4])
             if CURE.search(line) or (re.search(OPTION, line, re.I)
                                      and (READING.search(line) or FIGURE.search(line)))]
    assert not cures, f"M4 gives a cure from the pilot's figure: {cures}"


# A distance worked from the figure: an offset in Hz or kHz beside a reading in ppm, or beside a
# frequency in MHz unless the line poses it. The predict's "Say your radio sits about 16 kHz high
# at 100 MHz" supposes a distance and works none from the figure; "Say it reads +150 ppm: then
# 400 MHz lands 60 kHz high" works one. The check is also read whole, prompt, chips and whys
# together, because its chips carry the distances its prompt asks for.
OFFSET = re.compile(r"\d[\d,.]*\s*k?Hz\b", re.I)
TUNED = re.compile(r"\d[\d,.]*\s*MHz\b", re.I)


def _works_a_distance(line):
    """Whether a line works an offset from a reading, or from a tuned frequency it does not pose."""
    line = CONSTANTS.sub(" ", line)
    if not OFFSET.search(line):
        return False
    return bool(READING.search(line) or (TUNED.search(line) and not POSED.search(line)))


def test_m4_works_no_distance_from_the_pilots_figure(unit1_lessons):
    lesson = unit1_lessons[4]
    whole = []
    if lesson.check:
        parts = [f"{choice['label']}. {choice['why']}" for choice in lesson.check["choices"]]
        whole = [" ".join([lesson.check["prompt"], *parts])]
    worked = [line for line in _learner_lines(lesson) + whole if _works_a_distance(line)]
    assert not worked, f"M4 works a distance from the pilot's figure: {worked}"


# What M4 says in their place. The figure is the error of the clock that times the samples: a
# clause names a clock and the samples ("the sampling clock", "the clock that times the samples")
# beside the panel, the pilot or the figure, and nothing in the clause denies it. The dial has an
# error of its own: a sentence gives the dial or the tuner its own or a separate error, and
# nothing in the sentence denies it.
SAMPLE_CLOCK = re.compile(r"\bsampl\w*.*\b(?:clock|crystal|oscillator)s?\b"
                          r"|\b(?:clock|crystal|oscillator)s?\b.*\bsampl\w*", re.I)
MEASURED = re.compile(r"\b(?:panel|pilot|ppm|number|figure|reading)s?\b", re.I)
DENIED = re.compile(r"\b(?:not|never|no|nothing|without)\b|n't", re.I)
OWN = re.compile(r"\b(?:own|separate\w*)\b", re.I)


def test_m4_says_the_figure_is_the_sampling_clocks_and_the_dial_has_its_own(unit1_lessons):
    lines = _learner_lines(unit1_lessons[4])
    clock = [clause for line in lines for clause in _clauses(line)
             if SAMPLE_CLOCK.search(clause) and MEASURED.search(clause)
             and not DENIED.search(clause)]
    assert clock, "M4 must say its figure is the error of the clock that times the samples"
    dial = [line for line in lines
            if re.search(r"\b(?:dial|tuner)\b", line, re.I) and OWN.search(line)
            and re.search(r"\berrors?\b", line, re.I) and not DENIED.search(line)]
    assert dial, "M4 must say the dial (the tuner) has an error of its own, separate"
