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


def test_m11_try_puts_the_second_nodes_antenna_on_before_power():
    # The try asks for a transmission from a second node. A LoRa radio powered with no antenna on
    # its jack can be damaged, so the try says the antenna goes on first.
    lesson = load_lesson_file(PACK)
    ask = lesson.stages["try"].lower()
    assert re.search(r"\bantenna\b[^.?!]*\bbefore\b[^.?!]*\bpower", ask), (
        "the try must put the second node's antenna on before power")


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
