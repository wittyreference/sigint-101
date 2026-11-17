# ABOUTME: Tests the M6 "Antennas Are Tuning Forks" pack's resonance claim: an antenna off its
# ABOUTME: length hears worse, it is never deaf, and too long is never called as bad as too short.

import re
from pathlib import Path

from sigint101.lessons import load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "06-resonance.yaml"

# The pack called an antenna of the wrong length deaf, five times: the concept ("the wrong length
# is deaf"), the frame ("loud at one length, deaf everywhere else"), the demo (the stock whip
# "nearly deaf" at 1090 MHz), the try ("a deaf spot") and the explain ("The FM whip is deaf at
# 1090"). An antenna off its length hears worse; it is not deaf. RTL-SDR Blog, which makes a
# telescopic dipole kit, says almost any element length works for strong signals and fine tuning
# is for weak ones: whatever the length, the SWR stays below 5 at most frequencies, a loss of
# 2.5 dB or less (rtl-sdr.com, "Using our new dipole antenna kit", "Choosing the Antenna Element
# Length").
#
# The claim is refused in the words a rewrite reaches for: deaf or blind in any form; a dead,
# silent or mute spot or zone; hearing, catching, picking up or receiving nothing, none or no
# signal; any of those verbs negated with no degree after it ("can't hear 1090" fails, "can't
# hear it as well" passes); and dead, silent, mute or useless in one clause with the antenna, the
# whip, the dipole, a leg, an element, the length or "it". "When a band sounds dead" passes: it
# names the band, not the antenna.
HEAR = r"(?:hear\w*|catch\w*|caught|pick\w*\s+up|receiv\w*)"
DEAF = re.compile(r"\bdeaf\w*|\bblind\w*|\b(?:dead|silent|mute)\s+(?:spot|zone)s?\b"
                  rf"|\b{HEAR}\s+(?:nothing|none|no)\b", re.I)
NEGATED_HEAR = re.compile(r"(?:\b(?:cannot|never|not|no\s+longer|unable\s+to)|n['’]t)"
                          rf"\s+(?:\w+\s+)?{HEAR}(?P<rest>.*)", re.I)
DEGREE = re.compile(r"\b(?:as|so|well|much|clearly|nearly|quite|very|weak\w*|faint\w*)\b", re.I)
ANTENNA = re.compile(r"\b(?:antennas?|whips?|dipoles?|legs?|elements?|lengths?|it)\b", re.I)
NOTHING = re.compile(r"\b(?:dead|silent|mute|useless)\b|\bnothing\s+at\s+all\b", re.I)

# The pack once said "too long is every bit as deaf as too short". That is false both ways: a
# short antenna matches poorly, while a long one still receives, with a changed pattern. A
# sentence that sets too long beside too short must not call them equal: every bit as, just as,
# equally, as bad (or any one word) as, no better or no worse than, the same as.
LONG = re.compile(r"\b(?:too\s+long|longer|long)\b", re.I)
SHORT = re.compile(r"\b(?:too\s+short|shorter|short)\b", re.I)
EQUAL = re.compile(r"\bevery\s+bit\s+as\b|\bjust\s+as\b|\bequally\b|\bas\s+\w+\s+as\b"
                   r"|\bno\s+(?:better|worse)\s+than\b|\bthe\s+same\s+as\b", re.I)

# What the pack says instead: an antenna off its length hears worse. At least one line the
# learner reads must say so: a hearing word (hears, catches, picks up, receives, rings) with a
# lesser one after it, before the next full stop or semicolon (worse, poorly, poor, less, weaker,
# fainter, quieter, badly); poor or weaker hearing or reception; or the length costing or losing
# signal.
HEARS_WORSE = re.compile(
    r"\b(?:hear\w*|catch\w*|picks?\s+up|receiv\w*|rings?|ringing)\b[^.;?!]*?"
    r"\b(?:worse|poor\w*|less|weaker|fainter|quieter|badly)\b"
    r"|\b(?:poor\w*|worse|weaker|fainter)\s+(?:hearing|reception)\b"
    r"|\b(?:costs?|los(?:e|es|ing))\s+(?:you\s+)?(?:some\s+)?signal\b", re.I)


def _sentences(text):
    """A text's sentences: split after a full stop, question mark or bang and a space."""
    return [s for s in re.split(r"(?<=[.?!])\s+", text) if s]


def _learner_text(lesson):
    """Every sentence the learner reads in M6: title, concept, stages, verdict, chips, any check."""
    texts = [lesson.title, lesson.concept, *lesson.stages.values(), lesson.verdict,
             *(choice["label"] for choice in lesson.choices)]
    if lesson.check:
        texts.append(lesson.check["prompt"])
        texts.extend(choice["why"] for choice in lesson.check["choices"])
    return [s for text in texts for s in _sentences(text)]


def _clauses(sentence):
    """A sentence's clauses: split at a comma, semicolon, colon, dash, bracket, "but", "though"."""
    return [c.strip() for c in re.split(r"[,;:—–()]|\s(?:but|though)\s", sentence) if c.strip()]


def _deaf_claims(lesson):
    """Each sentence, or clause, of M6 that calls an antenna deaf in one of the wordings above."""
    claims = []
    for sentence in _learner_text(lesson):
        if DEAF.search(sentence):
            claims.append(sentence)
            continue
        for clause in _clauses(sentence):
            negated = NEGATED_HEAR.search(clause)
            if negated and not DEGREE.search(negated.group("rest")):
                claims.append(clause)
            elif ANTENNA.search(clause) and NOTHING.search(clause):
                claims.append(clause)
    return claims


def test_m6_never_calls_an_antenna_off_its_length_deaf():
    claims = _deaf_claims(load_lesson_file(PACK))
    assert not claims, f"M6 still calls an antenna off its length deaf: {claims}"


def test_m6_never_calls_too_long_as_bad_as_too_short():
    claims = [s for s in _learner_text(load_lesson_file(PACK))
              if LONG.search(s) and SHORT.search(s) and EQUAL.search(s)]
    assert not claims, f"M6 calls too long as bad as too short: {claims}"


def test_m6_says_an_antenna_off_its_length_hears_worse():
    lines = [s for s in _learner_text(load_lesson_file(PACK)) if HEARS_WORSE.search(s)]
    assert lines, "M6 must say an antenna off its length hears worse, not that it hears nothing"
