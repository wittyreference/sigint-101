# ABOUTME: Unit tests for the Flipper .sub RAW capture parser — a sub-GHz pulse train, from a file.
# ABOUTME: A malformed capture refuses with a reason; it is never silently repaired into a shape.

import pytest

from sigint101.subfile import SubFileError, parse_sub

# A well-formed RAW capture. Header keys and the FuriHal preset name are the format's own, and the
# timings are the documented example values from Flipper's SubGhzFileFormats.md — microseconds,
# positive for carrier on, negative for the gap, strictly alternating.
GOOD = """Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: RAW
RAW_Data: 413 -449 361 -420 413 -449 1201 -420 361 -1240
"""

# A capture split across lines, which the format permits: one line carries at most 512 timings,
# so any capture longer than that necessarily arrives in several.
SPLIT = """Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 315000000
Preset: FuriHalSubGhzPresetOok270Async
Protocol: RAW
RAW_Data: 413 -449 361
RAW_Data: -420 413 -449
"""


def _with_raw(values, frequency=433920000):
    return ("Filetype: Flipper SubGhz RAW File\nVersion: 1\n"
            f"Frequency: {frequency}\nPreset: FuriHalSubGhzPresetOok650Async\n"
            f"Protocol: RAW\nRAW_Data: {values}\n")


def test_a_well_formed_capture_parses():
    capture = parse_sub(GOOD)
    assert capture.frequency_hz == 433_920_000
    assert capture.preset == "FuriHalSubGhzPresetOok650Async"
    assert capture.protocol == "RAW"


def test_the_pulse_train_is_signed_microseconds():
    capture = parse_sub(GOOD)
    assert capture.pulses[:4] == (413, -449, 361, -420)
    # Sign is the carrier state, magnitude is the duration — that is the whole encoding.
    assert capture.pulses[0] > 0 and capture.pulses[1] < 0


def test_lines_are_concatenated_into_one_train():
    """512 timings per line is a file-format limit, not a signal boundary.

    Treating each line as a separate burst would split one transmission into two and put a
    false gap in the middle of the waveform the learner is reading.
    """
    capture = parse_sub(SPLIT)
    assert capture.pulses == (413, -449, 361, -420, 413, -449)


def test_a_leading_plus_is_accepted():
    # `subghz rx_raw` prints timings with an explicit +, the saved file omits it. Same data.
    assert parse_sub(_with_raw("+413 -449")).pulses == (413, -449)


def test_the_frequency_is_reported_in_hz():
    assert parse_sub(_with_raw("413 -449", frequency=315000000)).frequency_hz == 315_000_000


def test_total_duration_is_the_sum_of_the_magnitudes():
    # Gaps take real time; a duration that ignored them would understate the transmission.
    assert parse_sub(_with_raw("400 -600 400")).duration_us == 1400


def test_the_edge_count_is_the_number_of_transitions():
    assert parse_sub(_with_raw("400 -600 400 -600")).edge_count == 4


# --- refusals: a malformed capture is never quietly repaired -----------------------------

def test_a_train_starting_with_a_gap_is_refused():
    """The format requires the first value positive — a capture cannot begin mid-gap.

    Accepting it would shift every subsequent pulse into the wrong state, producing a waveform
    that renders cleanly and means something else entirely.
    """
    with pytest.raises(SubFileError):
        parse_sub(_with_raw("-413 449"))


def test_a_zero_duration_is_refused():
    # A zero-length pulse is not a pulse; the format forbids it outright.
    with pytest.raises(SubFileError):
        parse_sub(_with_raw("413 0 -449"))


def test_two_pulses_of_the_same_sign_in_a_row_are_refused():
    # Carrier cannot go on twice without going off between; this is a corrupt capture.
    with pytest.raises(SubFileError):
        parse_sub(_with_raw("413 361 -449"))


def test_more_than_the_line_limit_is_refused():
    with pytest.raises(SubFileError):
        parse_sub(_with_raw(" ".join(str(v if i % 2 == 0 else -v)
                                     for i, v in enumerate([100] * 513))))


def test_an_empty_capture_is_refused():
    with pytest.raises(SubFileError):
        parse_sub(_with_raw(""))


@pytest.mark.parametrize("missing", ["Filetype", "Frequency", "RAW_Data"])
def test_a_missing_required_header_is_refused(missing):
    text = "\n".join(line for line in GOOD.splitlines() if not line.startswith(missing))
    with pytest.raises(SubFileError):
        parse_sub(text + "\n")


def test_a_non_raw_protocol_is_refused():
    """A protocol-decoded .sub holds a key and bit count, not a pulse train.

    Reading one as RAW would find no timings; refusing names the real problem instead of
    reporting an empty waveform.
    """
    text = GOOD.replace("Protocol: RAW", "Protocol: Princeton")
    with pytest.raises(SubFileError):
        parse_sub(text)


def test_a_file_that_is_not_a_flipper_capture_is_refused():
    with pytest.raises(SubFileError):
        parse_sub("Filetype: Flipper SubGhz Key File\nVersion: 1\nFrequency: 433920000\n")


def test_a_non_numeric_timing_is_refused():
    with pytest.raises(SubFileError):
        parse_sub(_with_raw("413 -449 banana"))


def test_the_refusal_says_what_was_wrong():
    with pytest.raises(SubFileError) as caught:
        parse_sub(_with_raw("413 361"))
    message = str(caught.value).lower()
    # A refusal the learner cannot act on is just a failure.
    assert "sign" in message or "alternat" in message


# --- what the lesson reads off the train --------------------------------------------------

def test_pulse_widths_cluster_into_short_and_long():
    """The decode move: a fixed-code remote sends two symbol widths, and seeing that is the point.

    The learner is meant to notice the train is built from a small alphabet of durations, not a
    continuum — that is what makes it decodable at all.
    """
    capture = parse_sub(_with_raw("400 -400 400 -1200 1200 -400"))
    widths = capture.width_histogram()
    # Tolerance groups near-identical durations; a real capture never repeats an exact integer.
    assert sorted(widths) == [400, 1200]
    assert widths[400] == 4 and widths[1200] == 2
