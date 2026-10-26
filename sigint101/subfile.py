# ABOUTME: Reads a Flipper .sub RAW capture — the pulse train behind the sub-GHz waveform panel.
# ABOUTME: A malformed capture is refused with the reason; never repaired into a plausible shape.

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

# The header the Flipper writes at the top of a RAW capture. A key file (a decoded remote, stored
# as a protocol name and a key) carries a different Filetype and holds no pulse train at all, so
# the string is checked rather than assumed — reading one as RAW would report an empty waveform
# instead of naming the real mismatch.
FILETYPE = "Flipper SubGhz RAW File"

# Only a RAW capture holds timings. Every other Protocol value means the Flipper already decoded
# the transmission into a key and bit count, and threw the waveform away.
RAW_PROTOCOL = "RAW"

# A single RAW_Data line carries at most 512 timings. This is a line-length limit in the file
# format and not a boundary in the signal, so lines are concatenated: treating each as its own
# burst would insert a false gap into the middle of a transmission.
MAX_PER_LINE = 512

# Headers a capture cannot be read without. Preset and Protocol are checked separately because
# their *values* matter, not merely their presence.
REQUIRED = ("Filetype", "Frequency", "RAW_Data")

# Durations within this fraction of each other count as the same symbol width. A remote keys the
# same nominal pulse thousands of times and no two land on the same integer microsecond — the
# receiver's own sampling sees to that — so an exact-match histogram would report hundreds of
# distinct widths where the signal has two.
WIDTH_TOLERANCE = 0.15


class SubFileError(ValueError):
    """The capture is not one this parser can honestly read.

    Every refusal names what was wrong, because the reader is a learner holding a file they just
    recorded: "malformed" tells them nothing, "the train starts with a gap" tells them the
    capture began mid-transmission.
    """


@dataclass(frozen=True)
class SubCapture:
    """One RAW sub-GHz capture: what the radio was set to, and what it heard.

    Frozen because a capture is evidence. Nothing downstream should be able to adjust a timing
    and have the panel show the learner something the radio did not record.
    """

    frequency_hz: int
    preset: str
    protocol: str
    pulses: tuple

    @property
    def duration_us(self) -> int:
        """How long the whole transmission took, gaps included.

        Summed on magnitude: a gap occupies real time on the air, and a duration counting only
        the carrier-on pulses would understate the transmission by roughly half.
        """
        return sum(abs(p) for p in self.pulses)

    @property
    def edge_count(self) -> int:
        """How many times the carrier changed state — the transitions a decoder keys on."""
        return len(self.pulses)

    def width_histogram(self) -> dict:
        """How many times each symbol width appears, near-identical durations grouped.

        This is what the decode lesson reads off the train: a fixed-code remote is built from a
        small alphabet of durations — typically one short and one long — and noticing that the
        signal is not a continuum is what makes it decodable at all.
        """
        counts = Counter()
        for width in sorted(abs(p) for p in self.pulses):
            for known in counts:
                if abs(width - known) <= known * WIDTH_TOLERANCE:
                    counts[known] += 1
                    break
            else:
                counts[width] += 1
        return dict(counts)


def _headers(text: str) -> dict:
    """Collect the `Key: value` lines, keeping every RAW_Data line in order."""
    fields, raw_lines = {}, []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "RAW_Data":
            raw_lines.append(value)
        else:
            fields[key] = value
    if raw_lines:
        fields["RAW_Data"] = raw_lines
    return fields


def _timings(raw_lines) -> tuple:
    """Turn the RAW_Data lines into one validated pulse train.

    Validation is strict on purpose. Each rule below rejects a capture that would still render
    as a clean-looking waveform, which is the dangerous kind of corruption: the learner would be
    reading a picture of something the radio never heard.
    """
    pulses = []
    for line_number, line in enumerate(raw_lines, start=1):
        tokens = line.split()
        if len(tokens) > MAX_PER_LINE:
            raise SubFileError(
                f"RAW_Data line {line_number} holds {len(tokens)} timings; the format allows "
                f"at most {MAX_PER_LINE} per line")
        for token in tokens:
            # int() accepts a leading '+', which matters because `subghz rx_raw` prints timings
            # with the sign explicit while the saved file omits it. Same data, two spellings.
            try:
                value = int(token)
            except ValueError as exc:
                raise SubFileError(
                    f"RAW_Data line {line_number}: {token!r} is not a duration") from exc
            if value == 0:
                raise SubFileError(
                    f"RAW_Data line {line_number}: a zero-length pulse is not a pulse")
            pulses.append(value)

    if not pulses:
        raise SubFileError("the capture holds no timings — there is no waveform to read")
    # The three rules below all reject captures that would still draw a clean-looking waveform.
    # That is the dangerous kind of corruption: a file that fails loudly gets fixed, whereas one
    # that renders plausibly has the learner reading a picture of something the radio never heard.
    if pulses[0] < 0:
        raise SubFileError(
            "the pulse train starts with a gap, so the capture began mid-transmission; "
            "every following pulse would be shown in the wrong carrier state")
    for index in range(1, len(pulses)):
        if (pulses[index] > 0) == (pulses[index - 1] > 0):
            raise SubFileError(
                f"timings {index} and {index + 1} share a sign; the carrier cannot change to "
                "the state it is already in, so the signs must alternate")
    return tuple(pulses)


def parse_sub(text: str) -> SubCapture:
    """Read a Flipper RAW capture into the pulse train behind it.

    Takes the file's text rather than a path so the caller decides where a capture came from —
    pulled off the device over the serial CLI, or read from disk.
    """
    fields = _headers(text)
    # Presence is checked before any value is read, so a truncated or wrong-typed file is named
    # as such rather than failing later on a line that happens to be missing.
    for key in REQUIRED:
        if key not in fields:
            raise SubFileError(f"not a readable capture: no {key} line")

    if fields["Filetype"] != FILETYPE:
        raise SubFileError(
            f"this is a {fields['Filetype']!r}, not a {FILETYPE!r} — only a RAW capture "
            "carries a pulse train")
    protocol = fields.get("Protocol", "")
    if protocol != RAW_PROTOCOL:
        raise SubFileError(
            f"protocol is {protocol!r}, not {RAW_PROTOCOL}; the Flipper already decoded this "
            "one into a key and discarded the waveform")
    # Recorded, not validated against the CC1101's receive bands. A capture carries whatever the
    # radio was tuned to, and a file recorded on another Flipper in another region is still a
    # real capture worth reading — the console's job here is to report, not to police.
    try:
        frequency = int(fields["Frequency"])
    except ValueError as exc:
        raise SubFileError(f"frequency {fields['Frequency']!r} is not a number of hertz") from exc

    return SubCapture(frequency_hz=frequency, preset=fields.get("Preset", ""),
                      protocol=protocol, pulses=_timings(fields["RAW_Data"]))
