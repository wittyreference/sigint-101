# ABOUTME: Tests the M11 "Below the Noise Floor" LoRa lesson pack loads as a valid six-stage intro —
# ABOUTME: id 11, every stage non-empty, string choices, the LoRa (meshtastic) decode instrument.

import re
from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = Path(__file__).resolve().parent.parent / "lesson_packs" / "11-lora.yaml"


def test_m11_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 11
    assert lesson.slug == "lora"


def test_m11_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m11_needs_a_radio_and_does_not_transmit():
    # M11 reads packets the Nomad's LoRa radio received; node B does the transmitting, so the
    # console only receives and must not flag requires_tx.
    lesson = load_lesson_file(PACK)
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m11_uses_the_lora_instrument_not_the_waterfall():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_lora is True
    assert lesson.panels == ["lora"]


def test_m11_predict_choices_are_well_formed_string_key_label():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M11 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"


def test_m11_try_keeps_the_second_nodes_antenna_on():
    # The try asks for a transmission from a second node. A LoRa radio powered with no antenna on
    # its jack can be damaged, so the try keeps the antenna on. Any of three wordings will do:
    # "antenna always on", "antenna ... before ... power", or Meshtastic's own "never power ...
    # without ... antenna".
    lesson = load_lesson_file(PACK)
    ask = lesson.stages["try"].lower()
    assert re.search(r"\bantenna always on\b"
                     r"|\bantenna\b[^.?!]*\bbefore\b[^.?!]*\bpower"
                     r"|\bnever power\b[^.?!]*\bwithout\b[^.?!]*\bantenna\b", ask), (
        "the try must keep the second node's antenna on: 'antenna always on', "
        "'antenna ... before ... power' or 'never power ... without ... antenna'")


def test_m11_try_names_the_region_and_preset_the_second_node_shares_with_the_box():
    # A Meshtastic node sends nothing until its region is set, and nodes need the same region and
    # modem preset to communicate. Left unsaid, a mismatch is an empty list with no reason given.
    lesson = load_lesson_file(PACK)
    ask = lesson.stages["try"].lower()
    assert "region" in ask, "the try must name the region"
    assert "longfast" in ask, "the try must name the LongFast preset"


def test_m11_try_keeps_its_distance_and_the_path_with_no_second_node():
    # Fitting the try into its 60 words must not cost the half metre between the two radios, or
    # the line that tells a learner with no second node that an empty list is a result.
    lesson = load_lesson_file(PACK)
    ask = lesson.stages["try"]
    assert "50 cm" in ask
    assert "an empty list is the honest answer" in ask


def test_m11_try_measures_the_50_cm_from_the_nomad():
    # The half metre keeps a close transmitter from overloading the Nomad's own LoRa receiver, so
    # it is measured from the Nomad, and the try says so.
    lesson = load_lesson_file(PACK)
    assert "50 cm from the Nomad" in lesson.stages["try"], (
        "the try must measure the distance from the Nomad: '50 cm from the Nomad'")


# The preset as Meshtastic writes it, or its alt name, or its config name: "LongFast", "Long Fast",
# "LONG_FAST".
LONGFAST = re.compile(r"\blong[\s_-]?fast\b", re.I)

# LongFast's limit, about -17 dB: Semtech's datasheet gives -17.5 dB as the typical LoRa
# demodulator SNR at SF11, LongFast's spreading factor. Digits after a minus sign or a hyphen,
# "minus seventeen", or "seventeen dB under the noise".
SEVENTEEN = r"(?:17(?:\.5)?(?!\d)|seventeen\b)"
LONGFAST_LIMIT = re.compile(rf"(?:[\N{{MINUS SIGN}}-]\s?|\bminus\s+){SEVENTEEN}"
                            rf"|\b{SEVENTEEN}\s*dB\s+(?:below|under)\b", re.I)


# Fails first, gives up, drops or loses the packet, or needs a stronger signal.
FAILS = re.compile(r"\b(?:fail\w*|give\w* up|drop\w*|los(?:e|es|t|ing)\b"
                   r"|need\w* (?:a )?(?:strong|more))")


def _a_clause_has_faster_presets_failing(text):
    # One clause names the faster (or the short- or medium-range) presets and has them fail.
    for clause in re.split(r"[.;:!?]", text.lower()):
        if (re.search(r"\bpresets?\b", clause)
                and re.search(r"\b(?:faster|quicker|short\w*|medium|turbo)\b", clause)
                and FAILS.search(clause)):
            return True
    return False


def test_m11_predict_names_the_longfast_preset_but_not_its_limit():
    # Twelve dB under the noise decodes on LongFast (SF11, typical limit -17.5 dB) and fails on the
    # faster presets (SF7 stops near -7.5 dB), so the predict names the preset it asks about. It
    # gives no limit: "about -17 dB" there would hand the learner the answer before the commit.
    predict = load_lesson_file(PACK).stages["predict"]
    assert LONGFAST.search(predict), "the predict must name the LongFast preset"
    assert not re.search(rf"\b{SEVENTEEN}|\blimit", predict, re.I), (
        "the predict must not give LongFast's limit away")


def test_m11_verdict_gives_the_longfast_limit_and_faster_presets_failing_sooner():
    # The verdict says why the answer holds on this radio: on LongFast a packet decodes down to
    # about -17 dB, and a faster preset loses the same packet sooner.
    verdict = load_lesson_file(PACK).verdict
    assert re.search(r"\bdecodes\b", verdict, re.I), "the verdict must keep the answer: it decodes"
    assert LONGFAST.search(verdict), "the verdict must name the LongFast preset"
    assert LONGFAST_LIMIT.search(verdict), "the verdict must give LongFast's limit, about -17 dB"
    assert _a_clause_has_faster_presets_failing(verdict), (
        "the verdict must say the faster presets fail sooner")
