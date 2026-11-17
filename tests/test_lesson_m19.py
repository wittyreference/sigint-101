# ABOUTME: Tests the M19 "Catch the Catcher" pack loads as a valid six-stage intro — id 19, the
# ABOUTME: rayhunter instrument on a separate device, and the look-closer honesty at predict.

import re
from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "19-catch-the-catcher.yaml")

# The pack called the console's link to the Orbic "receive-only" (the try: "polls its page ...:
# receive-only"), the whole set-up the same ("awareness, bounded and receive-only") and, in its
# header, the Orbic "polled receive-only". Neither holds. The console reaches the page as a WiFi
# client, and a WiFi client transmits as well as receives. The Orbic is a working cellular
# hotspot that talks to the tower it is attached to: EFF's Rayhunter reads the control traffic
# between the hotspot and that tower. The label is refused anywhere in the pack file, comments
# included, in the words a trim reaches for: receive-, RX- or listen-only, only receives or
# listens, passive, one-way.
RECEIVE_ONLY = re.compile(
    r"\breceiv(?:e|es|ing)[\s-]+only\b|\brx[\s-]*only\b|\blisten(?:s|ing)?[\s-]+only\b"
    r"|\b(?:only|just|merely)\s+(?:receiv|listen)\w*|\bpassive(?:ly)?\b|\bone[\s-]way\b", re.I)

# The same claim as a sentence, as main had it before the trim: "You read; you never transmit."
# A clause that says nothing is transmitted, sent, emitted, radiated, broadcast, spoken or talked
# passes only when it names the console's side first (the console, you, its reads or polls) and is
# limited to the tower, which is the narrower claim that holds. One that names the Orbic, its
# hotspot, its modem or Rayhunter first fails whatever it is limited to, and so does a tower
# that never hears the Orbic.
NEGATION = r"(?:\b(?:never|not|no|nothing|none|without)\b|n't)"
SEND = (r"(?:transmi\w*|send\w*|sent|emit\w*|emission\w*|radiat\w*|broadcast\w*|talk\w*"
        r"|speak\w*|spoke\w*)")
NO_SEND = re.compile(rf"{NEGATION}.*?\b{SEND}\b|\b{SEND}\s+(?:nothing|none|no)\b", re.I)
TOWER = re.compile(r"\btowers?\b|\bcell(?:ular)?\s+network\b|\bbase\s+stations?\b", re.I)
ACTOR = re.compile(r"\b(?:(?P<orbic>orbic|hotspot|modem|rayhunter)|console|you|your|reads?"
                   r"|polls?|requests?)\b", re.I)
UNHEARD_ORBIC = re.compile(
    rf"\btowers?\b.*?{NEGATION}.*?\b(?:hears?|heard)\b.*?\b(?:orbic|hotspot|modem)\b"
    rf"|\b(?:orbic|hotspot|modem)\b.*?{NEGATION}.*?\bheard\b", re.I)

# What the pack says instead, as the A10 ruling put it: the console only reads Rayhunter's report
# (GETs of the manifest and the analysis report, sigint101/rayhunter.py), and what it reads never
# reaches a tower, since each GET ends at the Orbic's own web server. One sentence must name the
# console (or you) and say it only reads; one must say the reads, the polls or the console never
# reach, or are never heard by, a tower.
ONLY_READS = re.compile(r"\b(?:only|just|merely)\s+(?:reads?|reading|polls?|polling)\b"
                        r"|\breads?\s+only\b|\bread[\s-]only\b|\bnothing\s+but\s+read\w*", re.I)
CONSOLE = re.compile(r"\b(?:console|you)\b", re.I)
NOT_TO_TOWER = re.compile("|".join((
    rf"\btowers?\b[^.;:?!]*{NEGATION}[^.;:?!]*\b(?:reads?|polls?|requests?|console)\b",
    rf"{NEGATION}[^.;:?!]*\b(?:reads?|polls?|requests?|console|you)\b[^.;:?!]*\btowers?\b",
    rf"\b(?:reads?|polls?|requests?|console|you)\b.*?{NEGATION}.*\btowers?\b",
)), re.I)

# And what the pack left out: the Orbic itself is an ordinary cellular device that talks to
# towers. One sentence must have the Orbic (or the hotspot) talking, speaking, transmitting,
# sending, signalling, registering, attaching or connecting to a tower, or towers hearing it, with
# no negation between the two.
TALKS = (r"(?:talk\w*|speak\w*|transmi\w*|send\w*|signal\w*|register\w*|attach\w*"
         r"|connect\w*)")
ORBIC_TALKS = (rf"\b(?:orbic|hotspot)\b.*?\b{TALKS}\b.*?\btowers?\b"
               rf"|\btowers?\b.*?\b(?:hears?|heard)\b.*?\b(?:orbic|hotspot)\b")

# The Orbic's address, 192.168.1.1:8080, would end a clause at its dots and its colon.
ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?")


def _sentences(text):
    """A text's sentences: split after a full stop, question mark or bang and a space."""
    return [s for s in re.split(r"(?<=[.?!])\s+", ADDRESS.sub("the address", text)) if s]


def _learner_text(lesson):
    """Every sentence the learner reads in M19: concept, stages, verdict, chips and any check."""
    texts = [lesson.concept, *lesson.stages.values(), lesson.verdict,
             *(choice["label"] for choice in lesson.choices)]
    if lesson.check:
        texts.append(lesson.check["prompt"])
        texts.extend(choice["why"] for choice in lesson.check["choices"])
    return [s for text in texts for s in _sentences(text)]


def _clauses(sentence):
    """A sentence's clauses: split at a comma, semicolon, colon, dash, bracket, "but", "though"."""
    return [c.strip() for c in re.split(r"[,;:—–()]|\s(?:but|though)\s", sentence) if c.strip()]


def _says_nothing_is_sent(clause):
    """True when a clause says the Orbic is silent, or the console sends nothing at all."""
    if UNHEARD_ORBIC.search(clause):
        return True
    if not NO_SEND.search(clause):
        return False
    actor = ACTOR.search(clause)
    if actor is not None and actor.group("orbic"):
        return True
    return not TOWER.search(clause)


def _orbic_talks(sentence):
    """True when some span of a sentence has the Orbic talking to a tower, unnegated."""
    return any(not re.search(NEGATION, m.group(1), re.I)
               for m in re.finditer(rf"(?=({ORBIC_TALKS}))", sentence, re.I))


def test_m19_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 19
    assert lesson.slug == "catch-the-catcher"


def test_m19_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m19_uses_the_rayhunter_instrument_on_a_separate_device_not_the_sdr():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_rayhunter is True
    assert lesson.panels == ["rayhunter"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    # The Orbic running Rayhunter is its own device over WiFi, not the SDR tuner — no tune band.
    assert lesson.tune is None


def test_m19_offers_the_look_closer_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M19 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # A red flag is a heuristic, not proof — red means look closer, never a verdict.
    assert any(c["key"] == "look-closer" for c in lesson.choices)


def test_m19_carries_the_counter_surveillance_honesty_in_the_reveal():
    """Red is not proof and a green line never proves absence — the law past the predict gate."""
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    reveal = lesson.stages["explain"].lower()
    assert "look closer" in reveal
    assert "never means no" in reveal


def test_m19_never_calls_the_console_link_or_the_set_up_receive_only():
    labels = [m.group(0) for m in RECEIVE_ONLY.finditer(PACK.read_text(encoding="utf-8"))]
    assert not labels, f"M19 still calls its WiFi link or the whole set-up receive-only: {labels}"


def test_m19_never_says_the_console_or_the_orbic_sends_nothing():
    claims = [clause for sentence in _learner_text(load_lesson_file(PACK))
              for clause in _clauses(sentence) if _says_nothing_is_sent(clause)]
    assert not claims, f"M19 says the console or the Orbic sends nothing: {claims}"


def test_m19_says_the_console_only_reads_and_its_reads_never_reach_a_tower():
    sentences = _learner_text(load_lesson_file(PACK))
    assert any(CONSOLE.search(s) and ONLY_READS.search(s) for s in sentences), (
        "M19 must say the console only reads Rayhunter's report")
    assert any(NOT_TO_TOWER.search(s) for s in sentences), (
        "M19 must say that what the console reads never reaches a tower")


def test_m19_says_the_orbic_itself_talks_to_towers():
    sentences = _learner_text(load_lesson_file(PACK))
    assert any(_orbic_talks(s) for s in sentences), (
        "M19 must say the Orbic itself is a cellular device that talks to towers")
