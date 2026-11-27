# ABOUTME: Contract test over the shipped hardware lessons' instrument-stage narration: every demo,
# ABOUTME: try and observe fits the 800x480 band without scrolling (3x58, or 7x35 beside the radar).

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
INSTRUMENT_STAGES = ("demo", "try", "observe")
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
