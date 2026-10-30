# ABOUTME: Tests the M28 "A Message That Hops" pack loads as a valid six-stage intro — id 28, the
# ABOUTME: mesh layer read off the reused LoRa panel, and the relayed-onward answer at predict.

from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "28-meshtastic.yaml")


def test_m28_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 28
    assert lesson.slug == "meshtastic"


def test_m28_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m28_reads_the_mesh_off_the_reused_lora_panel():
    lesson = load_lesson_file(PACK)
    # M28 is the mesh/app layer above the M11 LoRa PHY; it reuses the LoRa panel and radio, so it
    # names no SDR tune band — it reads meshtasticd, never the dongle.
    assert lesson.uses_lora is True
    assert lesson.panels == ["lora"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    assert lesson.tune is None


def test_m28_offers_the_relayed_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M28 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # A mesh relays a packet node to node, so a message reaches a node beyond direct range.
    assert any(c["key"] == "relayed" for c in lesson.choices)


def test_m28_explains_the_hop_limit_and_relay_in_the_reveal():
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    reveal = lesson.stages["explain"].lower()
    assert "hop limit" in reveal
    assert "relay" in reveal


def test_m28_keeps_the_honest_encrypted_note_in_the_copy():
    """The lesson teaches the encrypted-channel case the LoRa panel already shows honestly."""
    lesson = load_lesson_file(PACK)
    assert "encrypted" in lesson.stages["demo"].lower()
