# ABOUTME: Tests M2 "Turn It Up" gives the best gain no usual place on the knob (it once said
# ABOUTME: "usually mid-scale"), and that its explain still sets the gain by the gap on the meter.

import re
from pathlib import Path

from sigint101.lessons import load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "02-gain-and-snr.yaml"

# The explain's rule to carry out the door said "set gain where the gap is widest, usually
# mid-scale". Where the gap is widest is a reading, not a place on the knob. RTL-SDR Blog's SDR#
# guide (rtl-sdr.com, "SDR# Users Guide", "Setting the RF Gain") sets the gain for the best
# signal-to-noise ratio: raise it until the noise floor starts rising faster than the signal, then
# stop. It names no usual setting. The noaa-apt guide's receiver settings for a NOAA pass start the
# LNA gain at maximum and leave it to be adjusted for the best signal-to-noise ratio. So no line
# the learner reads may give the best gain a usual place on the knob.
#
# MIDDLE is the middle of the knob's travel in the words a rewrite reaches for: mid-scale,
# mid-range, midway, midpoint, mid gain, mid, the middle, halfway, the centre of the range. The
# middle of the screen, the waterfall, the band or the meter is another thing. USUAL is a habit or
# a likelihood: usually, almost always, always, typically, generally, normally, mostly, most often,
# more often than not, often, commonly, as a rule, a rule of thumb, in most cases, tends to,
# likely, probably. BEST is the optimum: the best, optimal, ideal or winning setting, the sweet
# spot, the widest, biggest, largest or greatest gap.
MIDDLE = (r"(?:\bmid[- ]?(?:scale|range|way|point|dial|gain|setting|travel)\b|\bmid\b(?![-'’])"
          r"|\bmiddle\b(?!\s+of\s+(?:the\s+|your\s+)?(?:screen|waterfall|band|span|meter|display"
          r"|panel)\b)|\bhalf[- ]?(?:way|scale|gain|travel)\b"
          r"|\bcent(?:re|er)\s+of\s+the\s+(?:range|scale|dial|knob|travel)\b)")
USUAL = (r"(?:\busual(?:ly)?\b|\b(?:almost|nearly)\s+always\b|\balways\b|\btypical(?:ly)?\b"
         r"|\bgeneral(?:ly)?\b|\bnormal(?:ly)?\b|\bmostly\b|\bmost\s+(?:often|of\s+the\s+time)\b"
         r"|\bmore\s+often\s+than\s+not\b|\boften\b|\bcommon(?:ly)?\b|\bas\s+a\s+rule\b"
         r"|\brule\s+of\s+thumb\b|\bin\s+most\s+cases\b|\btends?\s+to\b|\blikely\b|\bprobably\b)")
BEST = (r"(?:\bbest\b|\boptim(?:al|um)\b|\bideal\b|\bsweet\s+spot\b|\bwins?\b"
        r"|\b(?:widest|biggest|largest|greatest)\b)")

# A negation within three words before a word, with no comma between, or right after it ("the
# middle is not", "the middle isn't"), takes the claim back: "not always mid-scale", "instead of
# the usual middle". "No rule, though it's usually mid-scale" keeps it.
NEG = (r"(?:\b(?:not|never|no|rarely|seldom|hardly|unlike)\b|n['’]t\b|\binstead\s+of\b"
       r"|\brather\s+than\b)")
NEG_BEFORE = re.compile(rf"{NEG}(?:\s+[\w'’-]+){{0,3}}\s*$", re.I)
NEG_AFTER = re.compile(r"^(?:\s+(?!(?:and|or|but)\b)[\w'’-]+)?\s+(?:(?:is|are|was|were|does|do"
                       r"|did|may|might|can|could|will|would|should)\s+)?(?:not|never|rarely"
                       r"|seldom)\b|^(?:\s+(?!(?:and|or|but)\b)[\w'’-]+)?\s+[a-z]+n['’]t\b", re.I)

# The claim, read in three ways.
# 1. In any line, a habit word next to the middle, with only words of place between them ("usually
#    mid-scale", "almost always somewhere in the middle", "often sits around the middle"), or the
#    middle with a habit word that closes the clause ("mid-scale, more often than not").
FILLER = (r"(?:is|are|was|be|it['’]s|that['’]s|sits?|lies|falls?|lands?|ends|up|found|at|in|on"
          r"|around|about|near|close|somewhere|roughly|right|just|to|the|a)")
SEP = r"[\s,—–]+"
HABIT_AT_MIDDLE = re.compile(rf"{USUAL}{SEP}(?:\b{FILLER}\b{SEP}){{0,5}}{MIDDLE}"
                             rf"|{MIDDLE}{SEP}(?:\b{FILLER}\b{SEP})?{USUAL}\s*(?:[.;:!?)]|$)", re.I)
# 2. In any line, one clause with a habit word, the optimum and the middle ("the gap is usually
#    widest mid-scale"). A clause that opens with "where", "which", a habit word or a bare place
#    ("mid-scale", "that's the middle") is read with the clause before it.
# 3. Outside the try, the predict and the observe, which send the learner to the middle setting to
#    read the meter there: one clause that puts the optimum at the middle ("the best gain is
#    mid-scale", "the sweet spot sits in the middle"), or an order to set, keep or leave the gain
#    there ("set gain mid-scale"), unless the clause makes it a condition or a possibility ("on a
#    busy band", "if", "may", "can").
INSTRUCTION_STAGES = ("try", "predict", "observe")
OPENS_ONTO_THE_LAST = re.compile(rf"^(?:where|which|wherever|whichever)\b|^{USUAL}"
                                 rf"|^(?:\b{FILLER}\b\s+|{USUAL}\s+)*{MIDDLE}[\s.;:!?]*$", re.I)
CONDITION = re.compile(r"\b(?:if|when|whenever|unless|may|might|can|could)\b"
                       r"|\b(?:on|in)\s+(?:a|the)\s+(?:busy|crowded|loud|noisy|quiet|strong)\b"
                       r"|\b(?:near|with|beside)\s+(?:a\s+)?strong\b", re.I)
ORDER = re.compile(r"^(?:(?:so|then|just|and|now|always)\s+)?(?:set|leave|keep|park|put|run|hold"
                   r"|choose|pick|aim)\s+(?:the\s+|your\s+)?(?:gain|it|knob|dial)\b"
                   r"(?:\s+(?:at|in|to|on|around|about|near|somewhere|roughly|close|right|the|a"
                   rf"|for)\b)*\s+{MIDDLE}", re.I)

# The rule itself stays: the explain must still tell the learner to set the gain by the gap. One
# of its sentences names the gain (or a setting, or the knob), the gap (or the SNR, the floor or
# the peak) and how to find the place: the widest, biggest, largest, greatest or best gap, a gap
# that stops growing, or a floor that rises faster than the peak.
GAIN = re.compile(r"\b(?:gain|setting|knob)\b", re.I)
GAP = re.compile(r"\b(?:gap|snr|floor|peak)\b", re.I)
GUIDE = re.compile(r"\b(?:widest|biggest|largest|greatest|best)\b"
                   r"|\bstops?\s+(?:growing|widening|opening)\b|\bfaster\s+than\b", re.I)


def _sentences(text):
    """A text's sentences: split after a full stop, question mark or bang and a space."""
    return [s for s in re.split(r"(?<=[.?!])\s+", text) if s]


def _learner_lines(lesson):
    """Every text a lesson shows, with where: title, concept, stages, verdict, chips and check."""
    lines = [("title", lesson.title), ("concept", lesson.concept), *lesson.stages.items(),
             ("verdict", lesson.verdict)]
    lines += [("chip", choice["label"]) for choice in lesson.choices]
    if lesson.check:
        lines.append(("check", lesson.check["prompt"]))
        lines += [("check", choice[field]) for choice in lesson.check["choices"]
                  for field in ("label", "why")]
    return lines


def _clauses(sentence):
    """A sentence's clauses: split at a comma, semicolon, colon, dash, bracket, "but", "though",
    with a clause that opens onto the one before it read together with that one."""
    clauses = []
    for part in re.split(r"[,;:—–()]|\s(?:but|though)\s", sentence):
        part = part.strip()
        if not part:
            continue
        if clauses and OPENS_ONTO_THE_LAST.search(part):
            clauses[-1] = f"{clauses[-1]} {part}"
        else:
            clauses.append(part)
    return clauses


def _affirmed(pattern, text):
    """Whether the pattern occurs in the text once with no negation just before or after it."""
    return any(not NEG_BEFORE.search(text[:m.start()]) and not NEG_AFTER.search(text[m.end():])
               for m in re.finditer(pattern, text, re.I))


def _places_the_best_gain(where, clause):
    """Whether one clause gives the best gain a usual place, or (outside an instruction stage)
    a place at all, at the middle of the knob."""
    if re.search(BEST, clause, re.I) and _affirmed(USUAL, clause) and _affirmed(MIDDLE, clause):
        return True
    if where in INSTRUCTION_STAGES or CONDITION.search(clause):
        return False
    return bool((re.search(BEST, clause, re.I) and _affirmed(MIDDLE, clause))
                or ORDER.search(clause))


def _usual_place_claims(lesson):
    """Each sentence, or clause, of M2 that gives the best gain a usual place on the knob."""
    claims = []
    for where, text in _learner_lines(lesson):
        for sentence in _sentences(text):
            if any(not NEG_BEFORE.search(sentence[:m.start()])
                   for m in HABIT_AT_MIDDLE.finditer(sentence)):
                claims.append(f"{where}: {sentence}")
                continue
            claims += [f"{where}: {clause}" for clause in _clauses(sentence)
                       if _places_the_best_gain(where, clause)]
    return claims


def test_m2_gives_the_best_gain_no_usual_place_on_the_knob():
    claims = _usual_place_claims(load_lesson_file(PACK))
    assert not claims, f"M2 gives the best gain a usual place on the knob: {claims}"


def test_m2_explain_still_sets_the_gain_by_the_gap():
    explain = load_lesson_file(PACK).stages["explain"]
    rules = [s for s in _sentences(explain) if GAIN.search(s) and GAP.search(s) and GUIDE.search(s)]
    assert rules, "M2's explain must still tell the learner to set the gain by the gap they read"
