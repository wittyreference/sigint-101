# ABOUTME: Tests what the M3 "Know Your Instrument" pack says a known station at its baseline shows:
# ABOUTME: the radio hears at that frequency, so a quiet band is likelier real, not proven empty.

import re
from pathlib import Path

from sigint101.lessons import load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "03-baseline.yaml"

# The explain said "Your reference settles it" and then, if the known station holds its baseline
# level, "the radio is healthy and the band is quiet". A steady FM station shows only that the
# radio still hears at that station's frequency, at that station's strength. A receiver's
# sensitivity, its ability to receive weak signals, depends on the frequency, which is why it is
# assessed over the receiver's whole range (ITU-R SM.1840-0 (12/2007): considering c), Annex 1 §1
# and §2). The course's own box showed FM stations well above its sweep floor while it was deaf
# at 1090 MHz and on 2 m (.claude/learnings.md; lesson 6: "The FM whip is deaf at 1090"). So a
# reference at its baseline makes a quiet band more likely real; it proves neither the band empty
# nor the radio healthy.

# A sentence speaks of the reference when it names it, the known station or the baseline, and so
# does the sentence after one ("Reference steady? Then ...").
ANCHOR = re.compile(r"\b(?:reference|station|baseline)s?\b", re.I)

# The claim, as the pack had it and in the words a trim reaches for. A quiet band: the band, the
# air, the sky or the spectrum is (really, truly, genuinely) quiet, empty, clear, silent or dead;
# a reference that means, proves, shows or confirms a quiet or empty band; the quiet, the silence
# or the emptiness is real, genuine or true. A healthy radio: the radio, receiver or instrument is
# healthy, fine, OK, good or working; a reference that means or proves a healthy or working radio;
# a radio that works, hears or receives, with no frequency to limit it. A settled question: the
# reference settles, resolves, decides, answers, proves or confirms it, or is the proof.
QUIET = r"(?:quiet|empty|clear|silent|dead|blank)"
BAND = r"(?:band|air|sky|spectrum|waterfall)"
QUIET_BAND = re.compile(
    rf"\b{BAND}\b.*\b(?:is|are|was|['’]s|be|really|truly|genuinely)\b.*\b{QUIET}\b"
    rf"|\b(?:means?|proves?|shows?|confirms?|guarantees?)\b.*\b{QUIET}\s+{BAND}\b"
    r"|\b(?:quiet|silence|emptiness|empty)\b.*\b(?:is|are|['’]s)\b.*\b(?:real|genuine|true)\b",
    re.I)
RADIO = r"(?:radio|receiver|instrument)"
HEALTHY_RADIO = re.compile(
    rf"\b{RADIO}\b.*\b(?:is|are|['’]s|stays?|remains?)\b.*\b(?:healthy|fine|ok|okay|good|sound"
    r"|well|alive|working)\b"
    rf"|\b(?:means?|proves?|shows?|confirms?|guarantees?)\b.*\b(?:healthy|working|sound)\s+{RADIO}\b"
    rf"|\b{RADIO}\b.*\b(?:works?|hears?|receives?)\b", re.I)
SETTLED = re.compile(r"\b(?:settle[sd]?|resolve[sd]?|decide[sd]?|answer(?:s|ed)?|prove[sd]?"
                     r"|confirm(?:s|ed)?)\s+(?:it|the\s+(?:question|matter|doubt))\b"
                     r"|\bis\s+(?:the\s+)?proof\b", re.I)

# What keeps a clause from stating the claim: a hedge on the quiet band (more likely, likelier,
# probably, not proven); a frequency the radio's hearing is limited to (at or on that or its
# frequency or channel, there, or the station itself); a negation; or a clause that only supposes
# (if, unless, whether, when, while).
HEDGE = re.compile(r"\b(?:more likely|likelier|likely|probabl[ey]|maybe|perhaps|may|might"
                   r"|unproven|not proven|not proof)\b", re.I)
AT_THAT_FREQUENCY = re.compile(
    r"\b(?:at|on)\s+(?:that|its|this|the\s+(?:reference|station)['’]s|the\s+same)\s+"
    r"(?:frequency|channel)\b|\bthere\b"
    r"|\b(?:hears?|receives?)\s+(?:that|the|your)\s+(?:known\s+)?(?:station|reference)\b", re.I)
NEGATION = re.compile(r"\b(?:not|never|no|nor|neither|nothing|cannot)\b|n['’]t\b", re.I)
SUPPOSES = re.compile(r"^(?:if|unless|whether|when|while)\b", re.I)

# What the explain says instead, as the A29 ruling put it: the radio hears at that frequency (the
# same limits as above), and a quiet band is only more likely real.
HEARS_AT_THAT_FREQUENCY = re.compile(
    r"\b(?:hears?|receives?|works?)\b[^,;:.]*?\b(?:at|on)\s+(?:that|its|this"
    r"|the\s+(?:reference|station)['’]s)\s+(?:frequency|channel)\b|\bhears?\s+there\b"
    r"|\bhears?\s+(?:that|the|your)\s+(?:known\s+)?(?:station|reference)\b", re.I)
QUIET_WORD = re.compile(r"\b(?:quiet|empty|silence|silent|emptiness|blank)\b", re.I)


def _sentences(text):
    """A text's sentences: split after a full stop, question mark or bang and a space."""
    return [s for s in re.split(r"(?<=[.?!])\s+", text.strip()) if s]


def _clauses(sentence):
    """A sentence's clauses: split at a comma, semicolon, colon, dash or bracket, and at "so",
    "but", "though" or "and"."""
    return [c.strip() for c in re.split(r"[,;:—–()]|\s(?:so|but|though|and)\s", sentence)
            if c.strip()]


def _learner_texts(lesson):
    """Every text the learner reads in M3: concept, stages, verdict, chips, check and its whys."""
    texts = [lesson.concept, *lesson.stages.values(), lesson.verdict,
             *(choice["label"] for choice in lesson.choices)]
    if lesson.check:
        texts.append(lesson.check["prompt"])
        for choice in lesson.check["choices"]:
            texts.extend((choice["label"], choice["why"]))
    return texts


def _about_the_reference(text):
    """A text's sentences that speak of the reference, and each sentence after one of them."""
    sentences = _sentences(text)
    return [s for i, s in enumerate(sentences)
            if ANCHOR.search(s) or (i > 0 and ANCHOR.search(sentences[i - 1]))]


def _states_the_claim(clause):
    """Whether a clause says, unhedged, that the band is quiet, the radio healthy or it settled."""
    if SUPPOSES.search(clause) or NEGATION.search(clause):
        return False
    if QUIET_BAND.search(clause) and not HEDGE.search(clause):
        return True
    if HEALTHY_RADIO.search(clause) and not AT_THAT_FREQUENCY.search(clause):
        return True
    return bool(SETTLED.search(clause))


def test_m3_never_says_a_steady_reference_proves_the_band_quiet_or_the_radio_healthy():
    lesson = load_lesson_file(PACK)
    claims = [clause for text in _learner_texts(lesson) for sentence in _about_the_reference(text)
              for clause in _clauses(sentence) if _states_the_claim(clause)]
    assert not claims, ("M3 says a reference at its baseline proves the band quiet, the radio "
                        f"healthy or the question settled: {claims}")


def test_m3_explain_says_a_steady_reference_shows_only_its_own_frequency():
    sentences = _sentences(load_lesson_file(PACK).stages["explain"])
    assert any(ANCHOR.search(s) and HEARS_AT_THAT_FREQUENCY.search(s) for s in sentences), (
        "the explain must say that a known station at its baseline shows the radio hears at that "
        "frequency")
    assert any(QUIET_WORD.search(c) and HEDGE.search(c) for s in sentences for c in _clauses(s)), (
        "the explain must call a quiet band more likely real, not proven")
