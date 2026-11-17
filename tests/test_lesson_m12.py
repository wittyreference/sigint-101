# ABOUTME: Tests the M12 "The Hidden Text" RDS/PSK lesson pack loads as a valid six-stage intro —
# ABOUTME: id 12, every stage non-empty, string choices, the RDS decode instrument, an FM-band tune.

import re
from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file
from sigint101.rds import DEFAULT_RDS_HZ, is_fm_channel

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "12-rds.yaml"

# The pack said three ways that a phase shift leaves the wave's height and frequency alone: the
# concept's "leaving the height and the frequency untouched", the observe's "changes neither
# height nor frequency" and the explain's "PSK holds both steady". For RDS it is false: the
# 57 kHz subcarrier is suppressed and amplitude-modulated by the shaped, biphase-coded data
# (NRSC-4-A §1.4; ITU-R BS.643-4 (12/2022), Annex 1, §1.4), so its height follows the data.
# PHASE finds a sentence about shifting the phase (PSK, phase-shift, a phase that shifts, flips,
# moves or changes), not one that only names the phase, so a true contrast such as "FM holds the
# height steady and moves the frequency, and with it the phase" is left alone. HELD_STEADY finds
# the claim in the wordings met so far: height, amplitude or frequency (or both) held steady,
# untouched, unchanged, constant, fixed, alone, the same or as they were; a shift that changes
# neither of them or does not change them; one that moves only the phase, or the phase and
# nothing else.
PHASE = re.compile(r"\bpsk\b|\bphase[- ]shift|\bshift(?:s|ed|ing)?\s+(?:only\s+)?(?:the\s+)?phase\b"
                   r"|\bflip(?:s|ped|ping)?\s+(?:the\s+)?phase\b"
                   r"|\bphase\s+(?:flips?|flipped|flipping|moves?|moved|moving|changes?|changed"
                   r"|changing)\b")
HELD_STEADY = re.compile("|".join((
    r"\bneither\s+(?:the\s+)?(?:height|amplitude|frequency)\b",
    r"\b(?:height|amplitude|frequency|both)\b[^.?!]*\b(?:steady|untouched|unchanged|constant|fixed"
    r"|alone|the\s+same|as\s+(?:it|they)\s+(?:was|were))\b",
    r"\b(?:height|amplitude|frequency|both)\b[^.?!]*\b(?:does\s+not|doesn['’]t|do\s+not|don['’]t"
    r"|never)\s+change",
    r"\b(?:does\s+not|doesn['’]t|do\s+not|don['’]t|never)\s+change[sd]?\b[^.?!]*"
    r"\b(?:height|amplitude|frequency)\b",
    r"\b(?:shifts?|moves?|changes?|flips?)\s+only\s+(?:the\s+)?phase\b",
    r"\bonly\s+(?:the\s+)?phase\s+(?:shifts|moves|changes|flips)\b",
    r"\b(?:shifts?|moves?|changes?|flips?)\s+(?:the\s+)?phase\s+and\s+nothing\s+else\b",
)))

# What the explain's sentence about the waterfall must name: the energy or the power, and not as
# something there is none of ("no energy").
ENERGY = re.compile(r"\b(?:energy|power)\b")
NO_ENERGY = re.compile(r"\bno\s+(?:\w+\s+)?(?:energy|power)\b")

# The predict said "RDS hides its data in the carrier's phase — not its height or frequency". That
# holds only when the subcarrier is read as two-phase PSK, the second view NRSC-4-A §1.4 allows.
# Both standards describe the data as amplitude-modulating a suppressed 57 kHz subcarrier
# (NRSC-4-A §1.4; ITU-R BS.643-4 (12/2022), Annex 1, §1.4), so the subcarrier's height follows the
# data, and the subcarrier deviates the station's own FM carrier (§1.3 of both), so the data moves
# the station's frequency too. In a sentence about the phase or PSK, RULES_OUT_HEIGHT finds the
# height, amplitude, pitch or frequency ruled out: "not", "never", "neither", "nor", "rather than",
# "instead of" or "without" just before it, or it "carries nothing"; or the data "only in the
# phase", "in the phase alone". A negation in front of the phase itself is left alone, so "eyes see
# amplitude and frequency, never phase" (true of a waterfall) passes, and so does "not only in the
# phase". In a sentence that also names the data, HELD_STEADY's wordings count too, so "hides its
# data in the phase, leaving its height and frequency untouched" fails; the FM contrast named for
# PHASE above names no data, and still passes.
ABOUT_PHASE = re.compile(r"\bphase|\bpsk\b")
DATA_WORD = re.compile(r"\b(?:data|bits?|text|hid(?:e|es|den|ing)|carr(?:y|ies|ied|ying)"
                       r"|rid(?:e|es|ing))\b")
RULES_OUT_HEIGHT = re.compile("|".join((
    r"(?:\b(?:not|never|neither|nor|rather\s+than|instead\s+of|without)|n['’]t)\s+"
    r"(?:(?:in|on|by|through)\s+)?(?:(?:its|the|their|any)\s+)?(?:\w+['’]s\s+)?"
    r"(?:height|amplitude|pitch|frequency)\b",
    r"\b(?:height|amplitude|pitch|frequency)\b[^.?!]*\b(?:carr(?:y|ies)|holds?|says?|tells?)\s+"
    r"(?:nothing|none|no)\b",
    r"(?<!not )\b(?:only|solely|purely|just)\s+in\s+(?:(?:its|the|their)\s+)?(?:\w+['’]s\s+)?"
    r"phase\b",
    r"(?<!not )\bin\s+(?:(?:its|the|their)\s+)?(?:\w+['’]s\s+)?phase\s+(?:alone|only)\b",
)))

# The demo had the subcarrier "flipping phase nearly twelve hundred times a second", the explain
# "flips phase 1187.5 times a second". 1187.5 is the bit rate: the clock is the subcarrier
# frequency divided by 48, so the basic data rate is 57 000 / 48 = 1187.5 bit/s (NRSC-4-A §1.5;
# ITU-R BS.643-4 (12/2022), Annex 1, §1.5). Each bit goes out as a biphase symbol (NRSC-4-A §1.7),
# so how often the phase flips depends on the data. BIT_RATE finds the figure as the pack has
# written it, or near it (1187.5, 1,188, 1,200, eleven or twelve hundred); wherever it stands, it
# must count bits. ONE_FLIP_PER_BIT finds the same claim without a number: a phase that flips once
# a bit, one flip per bit, every bit a flip.
BIT_RATE = re.compile(r"\b(?:1,?18[78](?:\.5)?|1,?200|(?:eleven|twelve)[ -]hundred)\b")
COUNTS_BITS = re.compile(r"\s+(?:\w+\s+)?(?:bits?\b|bit/s|bps\b)")
ONE_FLIP_PER_BIT = re.compile("|".join((
    r"\b(?:flip|revers|chang|shift)\w*\s+(?:(?:its|the)\s+)?phase\s+once\s+"
    r"(?:a|per|each|every|for\s+(?:each|every))\s+bit\b",
    r"\bphase\s+(?:flip|revers|chang|shift)\w*\s+once\s+(?:a|per|each|every|for\s+(?:each|every))"
    r"\s+bit\b",
    r"\b(?:a|one)\s+(?:phase\s+)?(?:flip|reversal)\s+(?:a|per|each|every|for\s+(?:each|every))"
    r"\s+bit\b",
    r"\b(?:each|every)\s+bit\s+is\s+(?:a|one)\s+(?:phase\s+)?(?:flip|reversal)\b",
)))


def _sentences(text):
    return re.split(r"(?<=[.?!])\s+", text.lower())


def _learner_lines(lesson):
    """(where, sentence) for every sentence the learner reads in M12: the title, the concept, each
    stage, the predict chips, the verdict, and a check's prompt, chips and whys if one is added."""
    texts = {"title": lesson.title, "concept": lesson.concept, **lesson.stages,
             "verdict": lesson.verdict}
    texts.update({f"chip {choice['key']}": choice["label"] for choice in lesson.choices})
    if lesson.check:
        texts["check"] = lesson.check["prompt"]
        texts.update({f"check {choice['key']}": f"{choice['label']}. {choice['why']}"
                      for choice in lesson.check["choices"]})
    return [(where, sentence) for where, text in texts.items() for sentence in _sentences(text)]


def test_m12_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 12
    assert lesson.slug == "rds"


def test_m12_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m12_needs_a_radio_and_does_not_transmit():
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m12_uses_the_rds_instrument_not_the_waterfall():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_rds is True
    assert lesson.panels == ["rds"]


def test_m12_tunes_to_an_fm_station():
    lesson = load_lesson_file(PACK)
    assert lesson.tune is not None, "M12 must point redsea at an FM station"
    assert 88_000_000 <= lesson.tune["center_hz"] <= 108_000_000


def test_m12_starts_its_dial_on_a_real_channel_the_decoder_defaults_to():
    # The site's station comes from SIGINT101_RDS_HZ and rds.js moves the dial to it; before the
    # first answer the dial shows the pack's band, which must be the same real FM channel.
    center = load_lesson_file(PACK).tune["center_hz"]
    assert is_fm_channel(center)
    assert center == DEFAULT_RDS_HZ


def test_m12_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M12 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"


def test_m12_never_says_a_phase_shift_holds_height_and_frequency_steady():
    lesson = load_lesson_file(PACK)
    texts = {"concept": lesson.concept, "verdict": lesson.verdict, **lesson.stages}
    claims = [f"{where}: {sentence}" for where, text in texts.items()
              for sentence in _sentences(text)
              if PHASE.search(sentence) and HELD_STEADY.search(sentence)]
    assert not claims, f"M12 still says a phase shift holds height and frequency steady: {claims}"


def test_m12_explain_says_the_waterfall_shows_the_energy_but_not_the_phase():
    # The waterfall paints one pixel per rtl_power bin, coloured by that bin's power (spectrum.py
    # SweepRow, waterfall.js drawRow): a PSK signal's energy shows, which phase each bit took never
    # does. "Invisible, every time" said a PSK signal never shows at all, against the explain's own
    # last line. The verdict above the explain carries the answer, so the explain needs no
    # "invisible" in any wording. One sentence must name the waterfall and the phase, and the
    # energy or the power without saying there is none; the wording around them stays free.
    explain = load_lesson_file(PACK).stages["explain"]
    assert "invisible" not in explain.lower(), "the explain must not call a PSK signal invisible"
    assert any("waterfall" in sentence and "phase" in sentence and ENERGY.search(sentence)
               and not NO_ENERGY.search(sentence) for sentence in _sentences(explain)), (
        "the explain must say, in one sentence, that a waterfall shows a PSK signal's energy (or "
        "power) and never its phase")


def test_m12_never_says_the_data_is_in_the_phase_and_not_the_height_or_frequency():
    lines = _learner_lines(load_lesson_file(PACK))
    claims = [f"{where}: {sentence}" for where, sentence in lines
              if ABOUT_PHASE.search(sentence) and (RULES_OUT_HEIGHT.search(sentence) or (
                  DATA_WORD.search(sentence) and HELD_STEADY.search(sentence)))]
    assert not claims, (
        f"M12 says the data is in the phase and not in the height or the frequency: {claims}")


def test_m12_gives_the_rds_rate_in_bits_never_as_phase_flips():
    lines = _learner_lines(load_lesson_file(PACK))
    flips = [f"{where}: {sentence}" for where, sentence in lines
             if any(not COUNTS_BITS.match(sentence, rate.end())
                    for rate in BIT_RATE.finditer(sentence))
             or ONE_FLIP_PER_BIT.search(sentence)]
    assert not flips, f"M12 gives RDS's bit rate as a rate of phase flips: {flips}"
