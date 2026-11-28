# ABOUTME: Tests the M29 "Meet the Flipper" pack loads as a valid six-stage intro — id 29, the
# ABOUTME: flipper instrument on a separate USB device, and the field-tool-plus-bench thesis.

import re
from pathlib import Path

from fastapi.testclient import TestClient

from sigint101.app import create_app
from sigint101.hardware import RadioProbe
from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "29-meet-flipper.yaml")


def test_m29_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 29
    assert lesson.slug == "meet-flipper"


def test_m29_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m29_uses_the_flipper_instrument_on_a_separate_device_not_the_sdr():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_flipper is True
    assert lesson.panels == ["flipper"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    # The Flipper is read over USB serial, its own device, not the SDR tuner — no tune band.
    assert lesson.tune is None


def test_m29_offers_the_bench_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M29 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # Plugged into the nomad the Flipper gains a bench (screen, storage, lessons), not more radios.
    assert any(c["key"] == "a-bench" for c in lesson.choices)


def test_m29_tile_reads_l30_and_badges_the_flipper_before_the_lesson_starts(tmp_path):
    """U10 follows the mesh in the teaching order, and its tile names the kit it needs."""
    with TestClient(create_app(pack_dir=PACK.parent, data_dir=tmp_path,
                               probe=lambda: RadioProbe(False, "lsusb", "none"))) as client:
        home = client.get("/").text
        unit = client.get("/unit/10").text
    assert 'href="/unit/10"' in home and "Flipper" in home
    assert re.search(r'tile__code">\s*L30\s*<', unit)
    assert 'href="/lesson/29"' in unit
    assert re.findall(r'tile__need">\s*([^<]*?)\s*<', unit) == ["Flipper"]


def test_m29_carries_the_field_tool_plus_bench_thesis_in_the_reveal():
    """Standalone it is the field tool; plugged into the nomad it gains the analyst's bench."""
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    reveal = lesson.stages["explain"].lower()
    assert "bench" in reveal
    assert "field tool" in reveal


# --- whose radio the panel calls alive ----------------------------------------------------------

# The panel prints "radio: alive" or "radio: down" (sigint101/static/flipper.js:20-21) from the
# device_info key radio_alive (sigint101/deviceinfo.py:17, :64). The Flipper's firmware writes that
# key from furi_hal_bt_is_alive() (flipperzero-firmware 1.4.3, targets/f7/furi_hal/
# furi_hal_info.c:197-199 and :297-298), whose header calls it the "BT/BLE system component state":
# true when core 2, the radio processor that runs the Bluetooth LE stack, is alive
# (targets/furi_hal_include/furi_hal_bt.h:146-150). The sub-GHz radio is another chip, a CC1101
# (the maker's tech specs, "Sub-1 GHz module"), and device_info reads nothing from it. The demo
# said the panel shows whether "its sub-GHz radio is alive".
#
# So no line the learner reads may give a radio state (alive, dead, up, down, running, ready, a
# state or a status) to a radio other than Bluetooth: the sub-GHz radio in any spelling, the
# CC1101, a band in MHz or kHz, NFC, RFID, infrared, LoRa or Wi-Fi. A state belongs to the nearest
# radio its sentence names before it, or after it when none comes before ("the state of its sub-GHz
# radio"), and to every radio joined to that one by "and", "or" or a comma ("its sub-GHz and
# Bluetooth radios"). A radio with a negation just before it ("not the sub-GHz one") is passed
# over. Bluetooth, BLE, 2.4 GHz, core 2 or the radio processor may own the state, and so may a
# radio named nothing more ("its radio state"), which the try reads as the demo's radio.
WRONG_RADIO = (r"\bsub[\s-]*(?:ghz|gigahertz|1[\s-]*ghz)\b|\bcc\s*1101\b"
               r"|\b\d{2,3}(?:\.\d+)?\s*[mk]hz\b|\bnfc\b|\brfid\b|\binfra[\s-]*red\b|\bir\b"
               r"|\blora\b|\bwi[\s-]*fi\b")
BLUETOOTH = (r"\bbluetooth(?:\s+(?:low\s+energy|le))?\b|\bble\b|\bbt\b|\b2\.4\s*ghz\b"
             r"|\bcore\s*(?:2|two)\b|\bradio\s+(?:processor|core|stack)\b|\bco-?processor\b")
RADIO_NOUN = (r"(?:[\s-]+(?:radios?|transceivers?|chips?|modules?|receivers?|sides?|ones?"
              r"|stacks?|cores?)\b)?")
RADIO = re.compile(rf"(?P<wrong>(?:{WRONG_RADIO}){RADIO_NOUN})"
                   rf"|(?P<bluetooth>(?:{BLUETOOTH}){RADIO_NOUN})"
                   r"|(?P<plain>\bradios?\b|\btransceivers?\b)", re.I)
# Up, down, on, off or live after "is" (or "'s") is a state only where it ends its clause or pairs
# with "or" or "and" ("is up or down", "is up and running"), so "it's up to you" and "the fob is
# on your keyring" are not.
STATE = re.compile(
    r"\b(?:alive|dead|healthy|awake|asleep|online|offline|running|working|ready|responding"
    r"|powered|states?|status(?:es)?)\b"
    r"|(?:\b(?:is|are|was|were|be|being|stays?|comes?|goes?)|['’]s)\s+(?:up|down|on|off|live)\b"
    r"(?=\s*(?:[.,;:!?)—–]|$)|\s+(?:or|and)\b)", re.I)
NEGATED = re.compile(r"(?:\b(?:not|never|no|nor|unlike|without)\b|n['’]t\b"
                     r"|\b(?:instead\s+of|rather\s+than|other\s+than)\b)(?:\s+[\w'’-]+){0,3}\s*$",
                     re.I)
JOINED = re.compile(r"^\s*,?\s*(?:(?:and|or|plus)\s+|[&/]\s*)?(?:(?:its|the|their)\s+)?$", re.I)


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


def _owners(sentence, state):
    """The radios a state word belongs to: the nearest one its sentence names before it, or after
    it when none comes before, with every radio joined to that one. A radio with a negation just
    before it is passed over; [] when no radio is left to own it."""
    named = [m for m in RADIO.finditer(sentence) if not NEGATED.search(sentence[:m.start()])]
    before = [m for m in named if m.end() <= state.start()]
    after = [m for m in named if m.start() >= state.end()]
    run = before[::-1] or after
    owners = run[:1]
    for near, far in zip(run, run[1:]):
        gap = sentence[far.end():near.start()] if before else sentence[near.end():far.start()]
        if not JOINED.match(gap):
            break
        owners.append(far)
    return owners


def _radio_states(lesson):
    """Each radio state a lesson names: where, its sentence, and the radios that own it."""
    return [(where, sentence, _owners(sentence, state))
            for where, text in _learner_lines(lesson) for sentence in _sentences(text)
            for state in STATE.finditer(sentence)]


def _states_given_to_another_radio(lesson):
    """Each sentence that gives a radio state to a radio other than Bluetooth."""
    return [f"{where}: {sentence}" for where, sentence, owners in _radio_states(lesson)
            if any(radio.group("wrong") for radio in owners)]


def _demo_radio_states(lesson):
    """The demo's sentences that give a radio state to the Bluetooth radio or a plain radio."""
    return [sentence for where, sentence, owners in _radio_states(lesson)
            if where == "demo" and owners and not any(r.group("wrong") for r in owners)]


def test_m29_gives_the_panels_radio_state_to_no_radio_but_bluetooth():
    claims = _states_given_to_another_radio(load_lesson_file(PACK))
    assert not claims, (
        f"M29 gives the panel's radio state to a radio other than Bluetooth: {claims}")


def test_m29_demo_still_says_the_panel_shows_whether_the_radio_is_alive():
    """The demo names the radio the panel reads; it does not drop the radio from the answer."""
    assert _demo_radio_states(load_lesson_file(PACK)), (
        "M29's demo must still say the panel shows whether the Flipper's radio is alive")
