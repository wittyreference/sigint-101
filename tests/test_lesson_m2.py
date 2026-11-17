# ABOUTME: Tests M2 "Turn It Up": no usual place on the knob for the best gain, more gain refused a
# ABOUTME: bigger gap (in M7, a fix) only past the widest one, and the gain still set by the gap.

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


# The rule to carry out the door ended "a bigger gap comes from a better or better-placed antenna,
# or getting closer, never more gain." Read alone, that says more gain never widens the gap, and it
# does while the gain sits below its best point. RTL-SDR Blog's quick start guide (rtl-sdr.com,
# "RTL-SDR Quick Start Guide") says the RF gain starts at zero, where the dongle hears little but
# strong broadcast FM, and to raise it until other signals appear. Its SDR# guide ("SDR# Users
# Guide", "Setting the RF Gain") says that as the gain rises a point comes where the noise floor
# climbs faster than the signal, and to stop raising the gain there. So more gain stops widening
# the gap only past the widest gap, and a line that refuses gain a bigger gap must tie it there.
#
# GAINS is gain in the words a rewrite reaches for: gain, more, extra, higher, added, excess or too
# much gain, the gain knob, turning, cranking or raising the gain (up). A refusal takes one of four
# shapes:
# 1. gain set aside as the source: "never more gain", "not from more gain", "rather than the gain
#    knob", "never by turning the gain up", "no more gain", "no amount of gain";
# 2. gain as the subject of a negated widening or fix, up to five words after it: "more gain never
#    widens the gap", "gain alone can't improve your SNR", "turning up the gain won't help", "the
#    gain knob can't buy you a bigger gap", "more gain won't fix either", or a bare "gain can't";
# 3. a gap that does not grow with gain: "the gap never grows with more gain", "neither is fixed by
#    more gain";
# 4. gain that widens, fixes, helps, buys or does neither, nothing or none: "more gain fixes
#    neither", "more gain does nothing for the gap".
GAINS = (r"\b(?:(?:the|any|more|extra|higher|added|additional|further|excess|surplus|too\s+much)"
         r"\s+)*gain\b(?:\s+knob\b)?"
         r"|\b(?:turning|cranking|raising|pushing|winding)\s+(?:up\s+)?(?:the\s+|your\s+)?gain\b"
         r"(?:\s+(?:up|higher)\b)?")
WIDEN = (r"(?:widen|grow|open|improve|raise|increase|lift|boost|help|stretch|enlarge|expand|fix"
         r"|cure|solve|mend)\w*")
GIVE = r"(?:make|give|buy|bring|add|win|get|earn)\w*"
GAP_WORD = r"(?:gap|snr|signal[- ]to[- ]noise|margin|wider|bigger|larger|it|that|this)"
WONT = (r"(?:(?:can|will|does|would|could)\s+)?never|can['’]?t|cannot|can\s+not|won['’]t"
        r"|will\s+not|doesn['’]t|does\s+not|didn['’]t|did\s+not|wouldn['’]t|couldn['’]t"
        r"|(?:is\s+not|isn['’]t)\s+going\s+to")
UP_TO_FIVE_WORDS = r"(?:,?\s+[\w'’-]+){0,5}?,?"
REFUSALS = (
    re.compile(rf"(?:\b(?:never|not|nor)\b|\brather\s+than\b|\binstead\s+of\b)"
               rf"(?:\s+(?:comes?|coming))?(?:\s+(?:from|by|through|with|via))?\s+(?:{GAINS})"
               r"|\bno\s+(?:(?:more|extra|added|additional|further|amount\s+of)\s+)+gain\b", re.I),
    re.compile(rf"(?:{GAINS}){UP_TO_FIVE_WORDS}\s+(?:{WONT})(?:\s+(?:ever|really|actually|truly))?"
               rf"(?:\s+{WIDEN}|\s+{GIVE}(?:\s+[\w'’-]+){{0,3}}?\s+{GAP_WORD}\b"
               r"|(?=\s*(?:[.;:!?)—–]|$)))", re.I),
    re.compile(r"(?:\b(?:never|not|cannot|neither|nothing|none)\b|n['’]t\b)[^.;:,—–]*?"
               rf"\b(?:with|from|by|through|via)\s+(?:{GAINS})", re.I),
    re.compile(rf"(?:{GAINS}){UP_TO_FIVE_WORDS}\s+(?:{WIDEN}|{GIVE}|do|does|did)(?:\s+you)?"
               r"\s+(?:neither|nothing|none)\b", re.I),
)

# A refusal holds when it is tied to the widest gap. Its sentence says where: past, beyond or after
# that point (or that setting, it, there, the widest, the best, the peak, the top), once, already,
# any further, further, from there, at that point (or the widest, the best, the very top), a bigger
# gap than that (or than the widest), or excess or too much gain, which is gain past the widest gap
# ("excess gain fixes neither"). Or it refuses gain only for a gap the sentence has just named as
# the widest, by "it", "that" or "this" ("set gain wherever your meter shows the widest gap; ... no
# more gain will widen it"). A check's chips and whys answer its prompt, so a prompt that already
# puts the gain at the widest gap ties them too ("your gain is already where the gap was widest").
SCOPE = re.compile(r"\b(?:past|beyond|after)\s+(?:(?:that|this|the|its|a|some|your)\s+)?"
                   r"(?:point|setting|spot|it|there|here|that|this|widest|best|peak|top|optimum"
                   r"|maximum)\b|\bonce\b|\balready\b|\bany\s+(?:further|more|wider)\b(?!\s+gain)"
                   r"|\bfurther\b(?!\s+gain)|\bfrom\s+(?:there|here|th(?:at|is)\s+point|then\s+on)\b"
                   r"|\bat\s+(?:that|this|the|its|your)\s+(?:very\s+)?(?:point|widest|best|top|peak"
                   r"|optimum|maximum)\b|\b(?:wider|bigger|larger|greater)\s+(?:gap\s+)?than\s+"
                   r"(?:that|this|it|the\s+widest)\b"
                   r"|\b(?:excess|surplus|too\s+much)\s+gain\b", re.I)
WIDEST = re.compile(r"\b(?:widest|biggest|largest|greatest|best)\b", re.I)
IT_OBJECT = re.compile(rf"^[^,.;:!?—–]*?\b(?:{WIDEN}|{GIVE})\s+(?:it|that|this)\b", re.I)


def _refusals(sentence):
    """Every place in a sentence that refuses more gain as a way to a bigger gap."""
    return [m for pattern in REFUSALS for m in pattern.finditer(sentence)]


def _tied(sentence, refusal, prompt=""):
    """Whether one refusal is tied to the widest gap: by its sentence, by the gap the sentence has
    just named as the widest, or by the check prompt the sentence answers."""
    if SCOPE.search(sentence) or (SCOPE.search(prompt) and WIDEST.search(prompt)):
        return True
    return bool(WIDEST.search(sentence[:refusal.start()])
                and IT_OBJECT.search(sentence[refusal.start():]))


def _untied_refusals(lesson):
    """Each sentence of a lesson that refuses more gain and does not tie it to the widest gap."""
    prompt = lesson.check["prompt"] if lesson.check else ""
    claims = []
    for where, text in _learner_lines(lesson):
        for sentence in _sentences(text):
            if any(not _tied(sentence, m, prompt if where == "check" else "")
                   for m in _refusals(sentence)):
                claims.append(f"{where}: {sentence}")
    return claims


def test_m2_refuses_more_gain_a_bigger_gap_only_past_the_widest_gap():
    claims = _untied_refusals(load_lesson_file(PACK))
    assert not claims, f"M2 says more gain never widens the gap, not only past the widest: {claims}"


# What the explain says instead: past the widest gap, more gain will not widen it. One explain
# sentence must hold a refusal tied by its own words, or say past that point (or once, further and
# the rest above) that more gain narrows, shrinks, closes, hurts, costs or loses the gap.
NARROWS = re.compile(rf"(?:{GAINS})[^.;:]*?\b(?:narrow\w*|shr[iu]nk\w*|clos(?:e|es|ed|ing)|hurt\w*"
                     r"|costs?|costing|los(?:e|es|ing))\b", re.I)


def test_m2_explain_still_says_more_gain_past_the_widest_gap_wont_widen_it():
    explain = load_lesson_file(PACK).stages["explain"]
    lines = [s for s in _sentences(explain)
             if any(_tied(s, m) for m in _refusals(s)) or (SCOPE.search(s) and NARROWS.search(s))]
    assert lines, "M2's explain must still say that past the widest gap more gain will not widen it"


# M7 "Build, Aim & Clean Up" made the same claim. Its rule to carry out the door ended "length and
# aim are fixed at the antenna, an overloading neighbour with a notch, and more gain fixes neither."
# For the overloading neighbour that holds at any gain: RTL-SDR Blog's SDR# guide ("What Overload
# Looks Like") gives turning the gain down as the first cure for overload. For a weak signal from
# the wrong length or aim it holds only past the widest gap, as above. So every line of M7 is read
# for the same refusals, which must be tied to the widest gap in the same ways, and its explain
# must still say that past that point more gain fixes neither.
M7_PACK = PACK.parent / "07-build-and-aim.yaml"


def test_m7_refuses_more_gain_a_fix_only_past_the_widest_gap():
    claims = _untied_refusals(load_lesson_file(M7_PACK))
    assert not claims, f"M7 says more gain fixes neither, not only past the widest gap: {claims}"


def test_m7_explain_still_says_more_gain_past_the_widest_gap_fixes_neither():
    explain = load_lesson_file(M7_PACK).stages["explain"]
    lines = [s for s in _sentences(explain)
             if any(_tied(s, m) for m in _refusals(s)) or (SCOPE.search(s) and NARROWS.search(s))]
    assert lines, "M7's explain must still say that past the widest gap more gain fixes neither"
