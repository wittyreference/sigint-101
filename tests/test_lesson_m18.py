# ABOUTME: Tests the M18 "The Camera's Fingerprint" pack loads as a valid six-stage intro —
# ABOUTME: id 18, the flock instrument, no SDR tune, the honesty answer, text true for both radios.

import re
from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs"
        / "18-flock-detector.yaml")


def _learner_lines(lesson):
    """Every line of lesson 18 the learner reads: title, concept, stages, verdict and chips."""
    yield "title", lesson.title
    yield "concept", lesson.concept
    yield from lesson.stages.items()
    yield "verdict", lesson.verdict
    for choice in lesson.choices:
        yield f"chip {choice['key']}", choice["label"]


# The site picks the Flock radio (SIGINT101_FLOCK_SOURCE): tcpdump on wlan1, a USB WiFi adapter, or
# Marauder on the Flipper's Wi-Fi devboard, read over its USB-UART bridge. The lesson's text is the
# same under both, so it may speak of a WiFi radio in monitor mode, but not of hardware or a tool
# only one source has: "a plain USB WiFi adapter" was false wherever the devboard listens.
WLAN1_ONLY = re.compile(
    r"\bwlan\d*\b|\btcpdump\b|\busb\b|\badapters?\b|\bdongles?\b|\brtl8821\w*", re.I)
MARAUDER_ONLY = re.compile(
    r"\bmarauder\b|\bdevboards?\b|\besp32\w*|\bflipper\b|\buart\b|\bbridges?\b|\bsniffprobe\b",
    re.I)

# tcpdump keeps every management frame (`type mgt`), but Marauder's sniffprobe buffers probe
# requests only: in its probe scan a beacon is dropped, buffered only by its PWN and AP scans
# (ESP32 Marauder v1.20.0, esp32_marauder/WiFiScan.cpp:9019-9087 and :9088-9144). A line saying
# the detector hears beacons, or every management frame, holds for wlan1 alone.
HEARS_MORE_THAN_PROBES = re.compile(
    r"\bbeacon\w*|\b(?:every|all|each|any)\s+(?:of\s+)?(?:the\s+)?(?:802\.11\s+|wi-?fi\s+)?"
    r"(?:management|mgmt|mgt)\s+frames?\b|\b(?:every|all)\s+(?:the\s+)?frames?\b", re.I)


def _one_source_hardware(text):
    """The words in a line that name hardware or a tool only one Flock source has."""
    return [m.group(0) for pattern in (WLAN1_ONLY, MARAUDER_ONLY) for m in pattern.finditer(text)]


def _hears_more_than_probes(text):
    """The words in a line that say the detector hears more than probe requests."""
    return [m.group(0) for m in HEARS_MORE_THAN_PROBES.finditer(text)]


def test_m18_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 18
    assert lesson.slug == "flock-detector"


def test_m18_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m18_uses_the_flock_instrument_on_a_separate_radio_not_the_sdr():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_flock is True
    assert lesson.panels == ["flock"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    # The Flock detector reads wlan1, not the SDR tuner, so the pack names no SDR tune band.
    assert lesson.tune is None


def test_m18_offers_the_honest_tiered_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M18 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        key = choice.get("key")
        label = choice.get("label")
        assert isinstance(key, str) and key, f"choice {choice!r} needs a non-empty string key"
        assert isinstance(label, str) and label, f"choice {choice!r} needs a non-empty string label"
    # An OUI prefix without the IE fingerprint is only a weak hint — that answer must be on offer.
    assert any(c["key"] == "weak-hint" for c in lesson.choices)


def test_m18_carries_the_counter_surveillance_honesty_law_in_the_reveal():
    """The "no alert never means no camera" law sits past the predict gate, in explain."""
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    assert "no alert never means no camera" in lesson.stages["explain"].lower()


def test_no_line_of_m18_names_hardware_only_one_flock_source_has():
    lesson = load_lesson_file(PACK)
    found = [f"{where}: {_one_source_hardware(text)}" for where, text in _learner_lines(lesson)
             if _one_source_hardware(text)]
    assert not found, f"lesson 18 names a radio or tool only one Flock source has: {found}"


def test_no_line_of_m18_says_the_detector_hears_more_than_probe_requests():
    lesson = load_lesson_file(PACK)
    found = [f"{where}: {_hears_more_than_probes(text)}" for where, text in _learner_lines(lesson)
             if _hears_more_than_probes(text)]
    assert not found, f"lesson 18 says the detector hears what only tcpdump keeps: {found}"
