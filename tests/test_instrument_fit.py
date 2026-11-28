# ABOUTME: Contract test over the shipped hardware lessons' instrument-stage narration: every demo,
# ABOUTME: try and observe fits the 800x480 band without scrolling (3x58, or 7x35 beside the radar).

import re
import textwrap
from pathlib import Path

import pytest

from sigint101.lessons import load_lessons

PACK_DIR = Path(__file__).resolve().parent.parent / "lesson_packs"
LESSONS = load_lessons(PACK_DIR)

# On the fixed 800x480 screen, an instrument stage's narration sits in a three-line band (~58
# characters wide) under the live instrument; Lesson 13's ADS-B radar stands beside the narration
# at full height (~7 lines of 35 characters). Lessons that need no radio (L5, L17, Unit 7) render
# their stages as scrollable prose and are outside this rule.
INSTRUMENT_STAGES = ("demo", "try", "operate", "observe")
BAND_WIDTH = 58
BAND_MAX_LINES = 3
RADAR_WIDTH = 35
RADAR_MAX_LINES = 7


def wrapped_lines(text: str, width: int) -> list:
    """Break narration into display lines at `width` without splitting words or hyphenated terms."""
    return textwrap.wrap(text.strip(), width=width,
                         break_long_words=False, break_on_hyphens=False)


def band_limits(lesson) -> tuple:
    """Return (width, max_lines) for an instrument stage of `lesson`."""
    if lesson.uses_adsb:
        return RADAR_WIDTH, RADAR_MAX_LINES
    return BAND_WIDTH, BAND_MAX_LINES


def fits_instrument_band(text: str, *, width: int = BAND_WIDTH,
                         max_lines: int = BAND_MAX_LINES) -> bool:
    """Whether `text` wraps within `max_lines` lines at `width` characters."""
    return len(wrapped_lines(text, width)) <= max_lines


def test_fits_instrument_band_accepts_three_lines_and_rejects_four():
    three = ("Watch — hands off. rtl_433 prints each device's make, reading and modulation: "
             "ASK, one frequency, or FSK, two. You're reading a signal you cannot see.")
    four = three + " One extra clause pushes this onto a fourth line."
    assert len(wrapped_lines(three, BAND_WIDTH)) == BAND_MAX_LINES
    assert fits_instrument_band(three)
    assert len(wrapped_lines(four, BAND_WIDTH)) == BAND_MAX_LINES + 1
    assert not fits_instrument_band(four)


def test_fits_instrument_band_uses_narrow_tall_budget_beside_radar():
    seven = " ".join(["word"] * 45)
    eight = " ".join(["word"] * 52)
    assert len(wrapped_lines(seven, RADAR_WIDTH)) == RADAR_MAX_LINES
    assert fits_instrument_band(seven, width=RADAR_WIDTH, max_lines=RADAR_MAX_LINES)
    assert len(wrapped_lines(eight, RADAR_WIDTH)) == RADAR_MAX_LINES + 1
    assert not fits_instrument_band(eight, width=RADAR_WIDTH, max_lines=RADAR_MAX_LINES)


def test_no_radio_lessons_are_excluded_from_the_band_contract():
    # Lessons 5 and 17 (and Unit 7's conceptual lessons) render as scrollable prose.
    hw_ids = {lesson_id for lesson_id, lesson in LESSONS.items() if lesson.needs_hardware}
    assert 5 not in hw_ids
    assert 17 not in hw_ids
    assert 13 in hw_ids
    assert band_limits(LESSONS[13]) == (RADAR_WIDTH, RADAR_MAX_LINES)
    assert band_limits(LESSONS[1]) == (BAND_WIDTH, BAND_MAX_LINES)


HARDWARE_STAGES = [
    (lesson_id, stage)
    for lesson_id in sorted(i for i, lesson in LESSONS.items() if lesson.needs_hardware)
    for stage in INSTRUMENT_STAGES
    if stage in LESSONS[lesson_id].stages
]


@pytest.mark.parametrize(("lesson_id", "stage"), HARDWARE_STAGES,
                         ids=lambda item: f"L{item:02d}" if isinstance(item, int) else item)
def test_hardware_instrument_narration_fits_band(lesson_id, stage):
    lesson = LESSONS[lesson_id]
    width, max_lines = band_limits(lesson)
    text = lesson.stages[stage]
    lines = wrapped_lines(text, width)
    assert len(lines) <= max_lines, (
        f"L{lesson_id:02d} {stage} wraps to {len(lines)}/{max_lines} lines at {width} chars "
        f"({len(text)} chars): {lines!r}"
    )


# Fidelity pins for instructions, questions, moved facts, and exact number forms across the trimmed
# hardware packs: each pin checks that trimming kept the original meaning and spelling intact.
FIDELITY_PINS = [
    ("L03-demo-whether-power-clean", 3, "demo",
     [r"shows the radio", r"whether\b[^.]*\bpower is clean"], [r",\s*clean power\b"]),
    ("L04-try-too-weak", 4, "try", [r"mono or too weak"], []),
    ("L04-observe-about-eleven", 4, "observe",
     [r"about eleven times", r"throw you off the signal\?"], []),
    ("L06-frame-aircraft", 6, "frame", [r"1090 MHz, where aircraft report positions"], []),
    ("L06-demo-number-form", 6, "demo",
     [r"Wavelength in metres is 300 over MHz: three at 100, about 0\.28 at 1090"], []),
    ("L07-demo-three-things-and-number", 7, "demo",
     [r"legs near 6\.9 cm", r"three things decide what gets in", r"88–108 MHz"],
     [r"88–108 MHz overload"]),
    ("L07-try-say-what-changed", 7, "try", [r"say what each\b[^.]*\bchanged"], []),
    ("L08-demo-ten-khz", 8, "demo", [r"under ten kHz"], [r"under 10 kHz"]),
    ("L08-try-hold-against-fm", 8, "try", [r"hold it against\b[^:]*\bFM"], []),
    ("L09-observe-go-back", 9, "observe", [r"go back to 433\.92"], []),
    ("L11-try-meshtastic-distance-few", 11, "try",
     [r"send a few", r"Meshtastic", r"at least 50 cm from the Nomad"], []),
    ("L12-demo-ps-eight-characters", 12, "demo",
     [r"the PS, eight characters"], [r"PS, 8 chars"]),
    ("L14-observe-question-preserved", 14, "observe",
     [r"how much\b[^?]*\bwaiting for something that knew the frame\?"], []),
    ("L18-demo-matched-against-shape", 18, "demo",
     [r"matched against a Flock camera's shape"], [r"matched to a Flock camera\b"]),
    ("L19-frame-colour-legend", 19, "frame",
     [r"green while recording cleanly", r"yellow, orange, then solid red as a warning grows",
      r"white when paused", r"low, medium or high"], []),
    ("L19-try-orbic-own-wifi", 19, "try",
     [r"over the Orbic's own WiFi"], [r"yellow–red"]),
    ("L24-demo-tone-on-fm-carrier", 24, "demo",
     [r"137 megahertz", r"2400-hertz tone on an FM carrier"], [r"2400 Hz FM tone"]),
    ("L24-observe-past-its-error-correction", 24, "observe",
     [r"past its error correction", r"fails its checksum"], []),
    ("L25-try-ariss-since-2026", 25, "try",
     [r"since 2026", r"ariss\.org lists each event's frequency and mode", r"\b437\.550\b"],
     [r"437\.550 MHz"]),
    ("L26-demo-aivdm-sentences", 26, "demo", [r"AIVDM sentences"], [r"AIVDM lines"]),
    ("L26-try-nine-digit-mmsi", 26, "try", [r"the nine-digit MMSI"], [r"9-digit MMSI"]),
    ("L28-try-read-not-key-node-shares", 28, "try",
     [r"not key", r"the node shares Long Turbo"],
     [r"text shares Long Turbo", r"\blong[\s_-]?fast\b"]),
    ("L29-observe-glance-flipper-both-ways", 29, "observe",
     [r"at a glance", r"Flipper's command line", r"both ways"], []),
]


@pytest.mark.parametrize(("pin_id", "lesson_id", "stage", "required", "forbidden"),
                         FIDELITY_PINS, ids=[pin[0] for pin in FIDELITY_PINS])
def test_trimmed_narration_preserves_pinned_instructions_and_numbers(
    pin_id, lesson_id, stage, required, forbidden
):
    text = LESSONS[lesson_id].stages[stage]
    for pattern in required:
        assert re.search(pattern, text, re.I), (
            f"{pin_id}: missing {pattern!r} in L{lesson_id:02d} {stage}: {text!r}"
        )
    for pattern in forbidden:
        assert not re.search(pattern, text, re.I), (
            f"{pin_id}: forbidden {pattern!r} in L{lesson_id:02d} {stage}: {text!r}"
        )
