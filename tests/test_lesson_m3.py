# ABOUTME: Tests what the M3 "Know Your Instrument" pack says a known station's level shows: held,
# ABOUTME: strong signals heard there and a likelier quiet band; dropped, never a deaf radio.

import re
from pathlib import Path

from sigint101.lessons import load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "03-baseline.yaml"

# The explain said "Your reference settles it" and then, if the known station holds its baseline
# level, "the radio is healthy and the band is quiet". A steady FM station shows only that the
# radio still hears at that station's frequency, at that station's strength. A receiver's
# sensitivity, its ability to receive weak signals, depends on the frequency, which is why it is
# assessed over the receiver's whole range (ITU-R SM.1840-0 (12/2007): considering c) and d),
# Annex 1 §1 and §2). The course's own box showed FM stations well above its sweep floor while it
# decoded no frame at 1090 MHz and no packet on 2 m (.claude/learnings.md). So a reference at its
# baseline makes a quiet band likelier real; it proves neither the band empty nor the radio
# healthy.

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
# probably, not proven); a frequency the radio's hearing is limited to (at or on that, its or this
# frequency or channel, the reference's or the station's, the reference frequency, "that one
# frequency", "there" but not "there is", or the station or reference it hears); a negation; or a
# clause that only supposes (if, unless, whether, when, while).
HEDGE = re.compile(r"\b(?:more likely|likelier|likely|probabl[ey]|maybe|perhaps|may|might"
                   r"|unproven|not proven|not proof)\b", re.I)
AT_FREQUENCY = (r"\b(?:at|on)\s+(?:that|its|this|the\s+(?:reference|station)(?:['’]s)?"
                r"|the\s+same)\s+(?:(?:one|same|single)\s+)?(?:frequency|channel)\b")
THERE = r"\bthere\b(?!\s+(?:is|are|was|were)\b|['’]s\b)"
HEARS_THE_STATION = (r"\b(?:hears?|receives?)\s+(?:that|the|your|its)\s+(?:\w+\s+)?"
                     r"(?:station|reference)\b")
AT_THAT_FREQUENCY = re.compile(f"{AT_FREQUENCY}|{THERE}|{HEARS_THE_STATION}", re.I)
NEGATION = re.compile(r"\b(?:not|never|no|nor|neither|nothing|cannot)\b|n['’]t\b", re.I)
SUPPOSES = re.compile(r"^(?:if|unless|whether|when|while)\b", re.I)

# What the explain says instead, as the A29 ruling put it: the radio hears at that frequency (the
# same limits as above), and a quiet band is only more likely real.
HEARS_AT_THAT_FREQUENCY = re.compile(
    rf"\b(?:hears?|receives?|works?)\b[^,;:.]*?{AT_FREQUENCY}|\bhears?\s+{THERE}"
    rf"|{HEARS_THE_STATION}", re.I)
QUIET_WORD = re.compile(r"\b(?:quiet|empty|silence|silent|emptiness|blank)\b", re.I)

# The ruling's other half: a station at its level shows the chain works "at that frequency and at
# that strong level", not that weak signals there still come through. On this console that limit
# bites: the panel counts the reference as holding until it falls more than 6 dB under its
# baseline median (sigint101/provenance.py, compare_to_baseline), and a loss in the antenna or the
# cable lowers every signal at that frequency alike. So the sentence that has the radio hearing at
# that frequency, or the one after it, must keep the hearing to strong signals (strong signals, a
# strong station, that strong level) or leave weak or faint signals unproven (named with a
# negation or a hedge).
STRONG = re.compile(r"\bstrong\s+(?:signals?|stations?|level)\b", re.I)
WEAK = re.compile(r"\b(?:weak|weaker|faint|fainter)\s+(?:signals?|stations?|ones)\b", re.I)


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


def _keeps_to_strong_signals(sentence):
    """Whether a sentence keeps the hearing to strong signals, or leaves weak ones unproven. Its
    parts split as clauses do, except at "and", so "weak signals and other bands stay unproven"
    keeps its hedge."""
    parts = re.split(r"[,;:—–()]|\s(?:so|but|though)\s", sentence)
    return any(STRONG.search(p) or (WEAK.search(p) and (NEGATION.search(p) or HEDGE.search(p)))
               for p in parts)


def test_m3_explain_keeps_a_steady_reference_to_strong_signals():
    sentences = _sentences(load_lesson_file(PACK).stages["explain"])
    hearing = [i for i, s in enumerate(sentences)
               if ANCHOR.search(s) and HEARS_AT_THAT_FREQUENCY.search(s)]
    assert any(_keeps_to_strong_signals(s) for i in hearing for s in sentences[i:i + 2]), (
        "the explain must say a held reference shows the radio hears strong signals at that "
        "frequency, or that weaker signals stay unproven")


# The explain went on: if the known station "has dropped or vanished while nothing else changed,
# the radio has gone deaf". The panel calls the reference dropped once it is more than 6 dB under
# its baseline median (sigint101/provenance.py, compare_to_baseline). A level 6 dB down is a
# quarter of the power and half the amplitude: a power level is 10 lg(P/P0) dB and a field level
# 20 lg(F/F0) dB (NIST Special Publication 811, §8.7), and 10 lg 0.25 = 20 lg 0.5 = -6.02. A
# radio that still hears the station at a quarter of its power hears worse there; it has not
# gone deaf, and a drop alone does not say how far its hearing fell.
#
# So a sentence of the explain that speaks of the reference dropping (it dropped, fell, sank,
# slipped, dipped, sagged, faded, vanished or lost; it is lower, weaker, fainter or down; it sits
# under or below its baseline), and the sentence after one, must not call the radio deaf: deaf or
# blind in any form; hearing, catching, picking up or receiving nothing (or next to nothing);
# losing its hearing; a radio, receiver, front end, antenna, cable or chain that is dead, silent,
# mute or useless; or a hearing verb negated with no degree after it ("can't hear there" fails,
# "can't hear it as well" passes). A clause with a negation before or within those words ("a drop
# is not deafness", "the radio is not dead") passes, and so does one that only supposes deafness
# ahead of any drop it names ("if the radio goes deaf, ...").
DROP = re.compile(r"\b(?:drop(?:s|ped|ping)?|fall(?:s|en|ing)?|fell|sinks?|sank|sunk"
                  r"|slip(?:s|ped|ping)?|dip(?:s|ped|ping)?|sag(?:s|ged|ging)?"
                  r"|fad(?:e|es|ed|ing)|vanish(?:es|ed|ing)?|disappear(?:s|ed|ing)?"
                  r"|los(?:t|es?|ing)|lower|weaker|fainter|down)\b"
                  r"|\b(?:under|below)\s+(?:its|the|your)\s+baseline\b", re.I)
HEAR = r"(?:hear\w*|catch\w*|caught|pick\w*\s+up|receiv\w*)"
NONE_HEARD = (r"(?:(?:almost|next\s+to|nearly)\s+)?"
              r"(?:nothing|none|no(?!\s+(?:better|more|worse|less|louder|clearer)\b))")
CHAIN = r"(?:radio|receiver|front\s+end|instrument|antenna|cable|chain|tuner|dongle)"
NO_USE = r"(?:dead|silent|mute|useless)"
DEAF = re.compile(rf"\bdeaf\w*|\bblind\w*|\b{HEAR}\s+{NONE_HEARD}\b"
                  r"|\blos(?:t|es?|ing)\s+(?:all\s+)?(?:its\s+|your\s+|the\s+)?hearing\b"
                  rf"|\b{CHAIN}\b.*\b{NO_USE}\b|\b{NO_USE}\s+{CHAIN}\b", re.I)
NEGATED_HEAR = re.compile(r"(?:\b(?:cannot|never|not|no\s+longer|unable\s+to)|n['’]t)"
                          rf"\s+(?:\w+\s+)?{HEAR}(?P<rest>.*)", re.I)
DEGREE = re.compile(r"\b(?:as|so|well|much|clearly|nearly|quite|very|weak\w*|faint\w*"
                    r"|strong\w*|full\w*|better|more|worse|less|louder|clearer)\b", re.I)
NOT = re.compile(r"\b(?:not|never|nor|neither|cannot)\b|n['’]t\b", re.I)


def _about_a_drop(text):
    """A text's sentences that speak of the reference dropping, and each sentence after one."""
    sentences = _sentences(text)
    return [s for i, s in enumerate(sentences)
            if DROP.search(s) or (i > 0 and DROP.search(sentences[i - 1]))]


def _calls_the_radio_deaf(clause):
    """Whether a clause calls the radio deaf in a wording above, with no negation before or within
    the words, unless it only supposes deafness ahead of any drop it names."""
    negated = NEGATED_HEAR.search(clause)
    if negated and not DEGREE.search(negated.group("rest")):
        at = negated.start()
    else:
        deaf = DEAF.search(clause)
        if not deaf or NOT.search(clause, 0, deaf.end()):
            return False
        at = deaf.start()
    if SUPPOSES.search(clause):
        drop = DROP.search(clause)
        return bool(drop) and drop.start() < at
    return True


def test_m3_explain_never_calls_a_dropped_reference_a_deaf_radio():
    about_a_drop = _about_a_drop(load_lesson_file(PACK).stages["explain"])
    assert about_a_drop, "the explain must still speak of a reference that has dropped"
    claims = [clause for sentence in about_a_drop for clause in _clauses(sentence)
              if _calls_the_radio_deaf(clause)]
    assert not claims, f"M3's explain calls a dropped reference a deaf radio: {claims}"


# The check scores "Your radio went deaf" for a reference 15 dB under its baseline. The explain
# already says the radio "hears worse there": a drop of that size (a thirtieth of the power,
# 10 lg 0.0316 = -15.0, NIST SP 811 §8.7) shows the radio hears that station worse, not that it
# hears nothing. So nothing the check shows (its prompt, chips and whys) calls the drop deafness,
# and the scored chip says the radio hears worse. The explain's own test above reads the same
# wordings (DEAF); here every clause of the check is read, since all of it speaks of the drop.
HEARS_WORSE = re.compile(
    r"\b(?:radio|receiver)\b.*\bhears?\s+(?:it\s+|that\s+\w+\s+)?worse\b", re.I)


def _check_texts(lesson):
    texts = [lesson.check["prompt"]]
    for choice in lesson.check["choices"]:
        texts.extend((choice["label"], choice["why"]))
    return texts


def test_m3_check_never_calls_a_15_db_drop_deafness():
    lesson = load_lesson_file(PACK)
    claims = [clause for text in _check_texts(lesson) for sentence in _sentences(text)
              for clause in _clauses(sentence) if _calls_the_radio_deaf(clause)]
    assert not claims, f"M3's check calls a dropped reference a deaf radio: {claims}"


def test_m3_check_scores_the_chip_that_says_the_radio_hears_worse():
    lesson = load_lesson_file(PACK)
    scored = [c for c in lesson.check["choices"] if c["key"] in lesson.check["answer"]]
    assert len(scored) == 1
    assert HEARS_WORSE.search(scored[0]["label"]), (
        f"the scored chip must say the radio hears worse: {scored[0]['label']!r}")
