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


def _sentences(text):
    return re.split(r"(?<=[.?!])\s+", text.lower())


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
