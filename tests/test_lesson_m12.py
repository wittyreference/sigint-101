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
# standard has the 57 kHz subcarrier suppressed and amplitude-modulated by the shaped,
# biphase-coded data (NRSC-4-A §1.4), so its height follows the data. A sentence that names the
# phase and holds height, amplitude or frequency steady, untouched or unchanged, or says they
# change neither, makes that claim.
PHASE = re.compile(r"\bphase\b|\bpsk\b")
HELD_STEADY = re.compile(r"\bneither\s+(?:the\s+)?(?:height|amplitude|frequency)\b"
                         r"|\b(?:height|amplitude|frequency|both)\b[^.?!]*"
                         r"\b(?:steady|untouched|unchanged)\b")


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
    # last line. One sentence must name the waterfall, the energy and the phase; only those three
    # words are held, so the wording around them stays free to change.
    explain = load_lesson_file(PACK).stages["explain"]
    assert "invisible, every time" not in explain.lower()
    assert any(all(word in sentence for word in ("waterfall", "energy", "phase"))
               for sentence in _sentences(explain)), (
        "the explain must say, in one sentence, that a waterfall shows a PSK signal's energy and "
        "never its phase")
