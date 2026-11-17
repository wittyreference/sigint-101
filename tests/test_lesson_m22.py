# ABOUTME: Tests the M22 "Two Streams" I/Q pack loads as a valid six-stage intro with no RF, and
# ABOUTME: that it gives I and Q as one sample against two references, never two readings in time.

import re
from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "22-iq-sampling.yaml"

# The demo said "The radio measures each sample twice, a quarter-cycle apart: I and Q", and the
# pack's header "the dongle hands up two streams a quarter-cycle apart". Both read as two readings
# taken a quarter-cycle apart in time. I and Q are two parts of one sample: the signal measured
# against two reference waves a quarter-cycle (90 degrees) apart, a cosine and a sine (PySDR, "IQ
# Sampling": quadrature is two waves 90 degrees out of phase, and each time step gives one I value
# and one Q value). With an R820T or R828D tuner the dongle digitises one real IF stream and forms
# both digitally (osmocom librtlsdr.c: Zero-IF mode off, In-phase ADC input only, a 3.57 MHz IF).
# A quarter-cycle (a quarter of a cycle, period, wave or turn, 90 degrees, pi/2) belongs to the
# nearest thing its sentence names before it, or after it when nothing comes before. A reference
# passes: a reference, an oscillator or LO, a carrier, waves (plural: "the wave" is the signal
# being read), a cosine or sine, or the floor, wall or axes of the clock-hand picture. A sample, a
# reading, a measurement, a stream, I or Q, "twice", "again", a moment, an instant or a time fails,
# and so does a quarter-cycle that names nothing.
QUARTER = re.compile(
    r"\bquarter[\s-]+(?:of\s+(?:an?|the|its)\s+)?(?:cycle|period|wave|turn)s?\b"
    r"|\b(?:90|ninety)[\s-]*(?:degrees?|deg)\b|\b90\s*°|\b(?:π|pi)\s*/\s*2\b", re.I)
SUBJECT = re.compile(
    r"(?P<reference>\b(?i:references?|oscillators?|carriers?|waves|cosines?|sines?"
    r"|floors?|walls?|axis|axes)\b|\bLOs?\b)"
    r"|\b(?i:sampl\w*|readings?|reads?|measur\w*|streams?|twice|again|moments?|instants?"
    r"|times?)\b|\b[IQ]\b")


def _sentences(text):
    """A text's sentences: split after a full stop, question mark or bang and a space."""
    return [s for s in re.split(r"(?<=[.?!])\s+", text) if s]


def _learner_text(lesson):
    """What the learner reads in M22, by place: title, concept, stages, verdict, chips, check."""
    texts = [("title", lesson.title), ("concept", lesson.concept), *lesson.stages.items(),
             ("verdict", lesson.verdict), *(("choice", c["label"]) for c in lesson.choices)]
    if lesson.check:
        texts.append(("check", lesson.check["prompt"]))
        texts.extend(("check", text) for c in lesson.check["choices"]
                     for text in (c["label"], c["why"]))
    return texts


def _comments(path):
    """The pack's own comments as one text, its ABOUTME header first: they tell whoever edits the
    pack what the lesson teaches."""
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return " ".join(re.sub(r"^#\s*(?:ABOUTME:\s*)?", "", line)
                    for line in lines if line.startswith("#"))


def _owners(sentence):
    """What each quarter-cycle in a sentence belongs to: the nearest thing named before it, or
    after it when nothing comes before; None when the sentence names nothing."""
    for quarter in QUARTER.finditer(sentence):
        before = list(SUBJECT.finditer(sentence, 0, quarter.start()))
        yield before[-1] if before else SUBJECT.search(sentence, quarter.end())


def _puts_a_quarter_cycle_between_readings(sentence):
    """True when a quarter-cycle in the sentence belongs to anything but a reference."""
    return any(owner is None or not owner.group("reference") for owner in _owners(sentence))


def _puts_a_quarter_cycle_between_references(sentence):
    """True when a quarter-cycle in the sentence belongs to a reference."""
    return any(owner is not None and owner.group("reference") for owner in _owners(sentence))


def test_m22_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 22
    assert lesson.slug == "iq-sampling"


def test_m22_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m22_is_a_no_rf_lesson():
    """I/Q is taught from the idea alone: no radio, no transmitter, no instrument panel."""
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is False
    assert lesson.requires_tx is False
    assert lesson.panels == []


def test_m22_never_puts_the_quarter_cycle_between_two_readings():
    texts = [*_learner_text(load_lesson_file(PACK)), ("comments", _comments(PACK))]
    claims = [f"{where}: {sentence}" for where, text in texts for sentence in _sentences(text)
              if _puts_a_quarter_cycle_between_readings(sentence)]
    assert not claims, f"M22 still has I and Q as two readings a quarter-cycle apart: {claims}"


def test_m22_says_the_quarter_cycle_lies_between_two_references():
    sentences = [s for _, text in _learner_text(load_lesson_file(PACK)) for s in _sentences(text)]
    assert any(_puts_a_quarter_cycle_between_references(s) for s in sentences), (
        "M22 must say each sample is measured against two references a quarter-cycle apart")
