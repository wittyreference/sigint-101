# ABOUTME: Pure data model for the /bands course band map: classifies every loaded lesson pack,
# ABOUTME: maps tuned frequencies to primary-source allocations, and computes dial positions.

from __future__ import annotations

import math
from dataclasses import dataclass

from .lessons import LessonError
from .operate import INSTRUMENTS, instrument
from .spectrum import DEFAULT_HIGH, DEFAULT_LOW, TUNER_MAX_HZ, TUNER_MIN_HZ

CAPTION = "Where each lesson tunes. A pin never means a signal on the air now."

DIAL_MIN_HZ = TUNER_MIN_HZ
DIAL_MAX_HZ = TUNER_MAX_HZ
DIAL_LEFT_PX = 18.0
DIAL_WIDTH_PX = 764.0

# Explanatory notes for lessons whose radio is not the RTL-SDR dial, keyed by pack `hardware:`.
RADIO_NOTE: dict[str, str] = {
    "lora": "Its node picks the channel; the course tunes nothing here.",
    "wifi": "A Wi-Fi radio, not this one.",
    "wifi-monitor": "A Wi-Fi radio, not this one.",
    "orbic-rayhunter": "The Orbic's own phone radio.",
    "flipper": "The Flipper's own radios.",
}


@dataclass(frozen=True)
class Allocation:
    """One cited band allocation heading on the course band map.

    `edge_hz` is set only for a printed table boundary whose owning row is unsettled (450.0 MHz,
    the shared edge of 420-450 and 450-454 MHz), so it matches that exact frequency while the
    neighbouring interval row stays strictly below it.
    """

    low_hz: int
    high_hz: int
    heading: str
    service: str
    cite: str
    edge_hz: int | None = None

    def contains_hz(self, hz: int) -> bool:
        if self.edge_hz is not None:
            return hz == self.edge_hz
        return self.low_hz <= hz < self.high_hz

    def contains_band(self, low_hz: int, high_hz: int) -> bool:
        if self.edge_hz is not None:
            return False
        return self.low_hz <= low_hz and high_hz <= self.high_hz


# Primary-source allocations only: codified 47 CFR 2.106 footnotes, or FCC Online Table of
# Frequency Allocations (rev. 2026-09-11) cross-checked against NTIA's June 2025 Manual.
ALLOCATIONS: tuple[Allocation, ...] = (
    Allocation(
        low_hz=88_000_000,
        high_hz=108_000_000,
        heading="88-108 MHz",
        service="FM broadcasting",
        cite="47 CFR 2.106(c)(93) (US93); 47 CFR 2.106(d)(5) (NG5)",
    ),
    Allocation(
        low_hz=117_975_000,
        high_hz=137_000_000,
        heading="117.975-137 MHz",
        service="aeronautical mobile",
        cite="47 CFR 2.106(b)(200) (5.200); 47 CFR 2.106(c)(26) (US26)",
    ),
    Allocation(
        low_hz=137_000_000,
        high_hz=138_000_000,
        heading="137-138 MHz",
        service="meteorological-satellite, space operation, space research, mobile-satellite",
        cite=(
            "FCC Table of Frequency Allocations, row 137-137.025 MHz (rev. 2026-09-11); "
            "rows 137.025-137.175, 137.175-137.825, 137.825-138 MHz; 5.208, US319, US320"
        ),
    ),
    Allocation(
        low_hz=144_000_000,
        high_hz=148_000_000,
        heading="144-148 MHz",
        service="amateur (non-Federal; amateur-satellite on 144-146; no Federal service)",
        cite=(
            "FCC Table of Frequency Allocations, row 144-146 MHz (rev. 2026-09-11); "
            "row 146-148 MHz (rev. 2026-09-11)"
        ),
    ),
    Allocation(
        low_hz=161_975_000,
        high_hz=162_025_000,
        heading="161.975 / 162.025 MHz",
        service="AIS 1 and AIS 2, Automatic Identification Systems",
        cite="47 CFR 2.106(c)(52) (US52)",
    ),
    Allocation(
        low_hz=420_000_000,
        high_hz=450_000_000,
        heading="420-450 MHz",
        service="amateur (non-Federal, secondary), radiolocation (Federal, primary)",
        cite=(
            "47 CFR 2.106(c)(270) (US270); 47 CFR 2.106(c)(269) (US269); "
            "47 CFR 2.106(b)(282) (5.282); 47 CFR 2.106(b)(138) (5.138)"
        ),
    ),
    Allocation(
        low_hz=450_000_000,
        high_hz=450_000_000,
        heading="450.0 MHz · edge of 420-450 and 450-454 MHz",
        service=(
            "420-450 MHz (non-Federal amateur, Federal radiolocation) and "
            "450-454 MHz (non-Federal land mobile)"
        ),
        cite=(
            "FCC Table of Frequency Allocations, row 420-450 MHz (rev. 2026-09-11); "
            "row 450-454 MHz (rev. 2026-09-11); 5.286, US87"
        ),
        edge_hz=450_000_000,
    ),
    Allocation(
        low_hz=929_000_000,
        high_hz=930_000_000,
        heading="929-930 MHz",
        service="land mobile and fixed (non-Federal; no Federal service)",
        cite="FCC Table of Frequency Allocations, row 929-930 MHz (rev. 2026-09-11); US116, US268",
    ),
    Allocation(
        low_hz=960_000_000,
        high_hz=1_215_000_000,
        heading="960-1215 MHz",
        service="aeronautical radionavigation",
        cite=(
            "47 CFR 2.106(b)(328) (5.328); 47 CFR 2.106(c)(224) (US224); "
            "47 CFR 2.106(c)(78) (US78)"
        ),
    ),
)


@dataclass(frozen=True)
class BandRow:
    """One lesson entry in the band-map ladder."""

    lesson_id: int
    title: str
    kind: str  # "tuned" | "default_sweep" | "off-dial"
    low_hz: int | None
    high_hz: int | None
    center_hz: int | None
    tool: str | None
    instrument_label: str | None
    fixed_hz: tuple[int, ...] = ()
    status: str | None = None
    note: str = ""


@dataclass(frozen=True)
class BandGroup:
    """A cited allocation heading and the sorted lesson rows under it."""

    allocation: Allocation
    rows: tuple[BandRow, ...]


@dataclass(frozen=True)
class DialTick:
    """One distinct tuned frequency on the decorative log-scale overview dial."""

    hz: int
    x: float
    replay_only: bool


@dataclass(frozen=True)
class DialBand:
    """The filled 88-108 MHz bar on the overview dial for default-sweep lessons."""

    low_hz: int
    high_hz: int
    x_start: float
    x_end: float


@dataclass(frozen=True)
class DialLabel:
    """One axis tick label on the overview dial."""

    text: str
    x: float
    anchor: str  # "start" | "middle" | "end"


@dataclass(frozen=True)
class BandMap:
    """The complete band map derived from the loaded lesson packs."""

    groups: tuple[BandGroup, ...]
    dial_rows: tuple[BandRow, ...]
    off_dial: tuple[BandRow, ...]
    no_radio: tuple[int, ...]
    no_radio_line: str
    ticks: tuple[DialTick, ...]
    fm_bar: DialBand | None
    axis_labels: tuple[DialLabel, ...]


def tick_x(hz: int) -> float:
    """Map a frequency in Hz onto the 764 px log-scale overview dial (x in [18, 782])."""
    span = math.log10(DIAL_MAX_HZ) - math.log10(DIAL_MIN_HZ)
    frac = (math.log10(hz) - math.log10(DIAL_MIN_HZ)) / span
    return round(DIAL_LEFT_PX + frac * DIAL_WIDTH_PX, 1)


def _format_mhz(hz: int) -> str:
    return f"{hz // 1_000_000}.{hz % 1_000_000:06d}".rstrip("0").rstrip(".")


def format_lesson_ids(ids: list[int] | tuple[int, ...]) -> str:
    """Format no-radio lesson ids as a compact line, collapsing runs of 3+ (e.g. M20-M23)."""
    if not ids:
        return ""
    ordered = sorted(ids)
    parts: list[str] = []
    start = prev = ordered[0]
    for lid in ordered[1:]:
        if lid == prev + 1:
            prev = lid
            continue
        parts.append(_range_token(start, prev))
        start = prev = lid
    parts.append(_range_token(start, prev))
    return "No radio in: " + ", ".join(parts)


def _range_token(start: int, end: int) -> str:
    if end - start >= 2:
        return f"M{start}-M{end}"
    if end == start + 1:
        return f"M{start}, M{end}"
    return f"M{start}"


def _instrument_for_lesson(lesson):
    """Join a lesson's panels onto operate.INSTRUMENTS by inst.api, defaulting to waterfall."""
    for inst in INSTRUMENTS:
        if inst.api and inst.api in lesson.panels:
            return inst
    return instrument("waterfall")


def _allocation_for_row(row: BandRow) -> Allocation:
    """Find the single cited allocation heading owning `row`, or an uncited band-edges fallback."""
    if row.kind == "tuned":
        assert row.center_hz is not None
        for alloc in ALLOCATIONS:
            if alloc.edge_hz is not None and alloc.contains_hz(row.center_hz):
                return alloc
        for alloc in ALLOCATIONS:
            if alloc.edge_hz is None and alloc.contains_hz(row.center_hz):
                return alloc
        return Allocation(
            low_hz=row.center_hz,
            high_hz=row.center_hz,
            heading=f"{_format_mhz(row.center_hz)} MHz",
            service="",
            cite="",
        )
    assert row.low_hz is not None and row.high_hz is not None
    for alloc in ALLOCATIONS:
        if alloc.contains_band(row.low_hz, row.high_hz):
            return alloc
    return Allocation(
        low_hz=row.low_hz,
        high_hz=row.high_hz,
        heading=f"{_format_mhz(row.low_hz)}-{_format_mhz(row.high_hz)} MHz",
        service="",
        cite="",
    )


def _note_for_off_dial(lesson) -> str:
    for hw in lesson.hardware:
        if hw in RADIO_NOTE:
            return RADIO_NOTE[hw]
    names = ", ".join(lesson.hardware)
    raise LessonError(
        f"lesson {lesson.id}: hardware {names!r} has no band-map note in RADIO_NOTE"
    )


def build_map(lessons: dict) -> BandMap:
    """Classify every loaded lesson into the dial ladder, off-dial list, or no-radio line."""
    dial_rows: list[BandRow] = []
    off_dial: list[BandRow] = []
    no_radio: list[int] = []

    for lid in sorted(lessons):
        lesson = lessons[lid]
        bm = getattr(lesson, "band_map", None) or {}
        status = bm.get("status")
        note = bm.get("note", "")

        if not lesson.hardware:
            no_radio.append(lesson.id)
            continue

        if lesson.tune is not None:
            center = lesson.tune["center_hz"]
            inst = _instrument_for_lesson(lesson)
            dial_rows.append(
                BandRow(
                    lesson_id=lesson.id,
                    title=lesson.title,
                    kind="tuned",
                    low_hz=center,
                    high_hz=center,
                    center_hz=center,
                    tool=inst.tool if inst else None,
                    instrument_label=inst.label if inst else None,
                    fixed_hz=tuple(inst.fixed_hz) if inst else (),
                    status=status,
                    note=note,
                )
            )
            continue

        if "rtl-sdr" in lesson.hardware:
            inst = _instrument_for_lesson(lesson)
            dial_rows.append(
                BandRow(
                    lesson_id=lesson.id,
                    title=lesson.title,
                    kind="default_sweep",
                    low_hz=DEFAULT_LOW,
                    high_hz=DEFAULT_HIGH,
                    center_hz=None,
                    tool=inst.tool if inst else None,
                    instrument_label=inst.label if inst else None,
                    fixed_hz=tuple(inst.fixed_hz) if inst else (),
                    status=status,
                    note=note,
                )
            )
            continue

        off_note = note or _note_for_off_dial(lesson)
        off_dial.append(
            BandRow(
                lesson_id=lesson.id,
                title=lesson.title,
                kind="off-dial",
                low_hz=None,
                high_hz=None,
                center_hz=None,
                tool=None,
                instrument_label=None,
                status=status,
                note=off_note,
            )
        )

    dial_rows.sort(key=lambda r: (r.low_hz, r.lesson_id))
    off_dial.sort(key=lambda r: r.lesson_id)
    no_radio.sort()

    grouped: dict[Allocation, list[BandRow]] = {}
    for row in dial_rows:
        alloc = _allocation_for_row(row)
        grouped.setdefault(alloc, []).append(row)

    groups = tuple(
        BandGroup(allocation=alloc, rows=tuple(rows))
        for alloc, rows in sorted(
            grouped.items(),
            key=lambda item: (item[0].low_hz, item[0].high_hz, item[0].edge_hz is not None),
        )
    )

    by_hz: dict[int, list[BandRow]] = {}
    for row in dial_rows:
        if row.kind == "tuned" and row.center_hz is not None:
            by_hz.setdefault(row.center_hz, []).append(row)
    ticks = tuple(
        DialTick(
            hz=hz,
            x=tick_x(hz),
            replay_only=all(r.status == "replay-only" for r in rows),
        )
        for hz, rows in sorted(by_hz.items())
    )

    has_default = any(row.kind == "default_sweep" for row in dial_rows)
    fm_bar = (
        DialBand(
            low_hz=DEFAULT_LOW,
            high_hz=DEFAULT_HIGH,
            x_start=tick_x(DEFAULT_LOW),
            x_end=tick_x(DEFAULT_HIGH),
        )
        if has_default
        else None
    )

    axis_labels = (
        DialLabel("24 MHz", DIAL_LEFT_PX, "start"),
        DialLabel("100", tick_x(100_000_000), "middle"),
        DialLabel("300", tick_x(300_000_000), "middle"),
        DialLabel("1000", tick_x(1_000_000_000), "middle"),
        DialLabel("1766", DIAL_LEFT_PX + DIAL_WIDTH_PX, "end"),
    )

    return BandMap(
        groups=groups,
        dial_rows=tuple(dial_rows),
        off_dial=tuple(off_dial),
        no_radio=tuple(no_radio),
        no_radio_line=format_lesson_ids(no_radio),
        ticks=ticks,
        fm_bar=fm_bar,
        axis_labels=axis_labels,
    )
