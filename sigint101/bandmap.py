# ABOUTME: Pure data model for the /bands course band map: classifies every loaded lesson pack,
# ABOUTME: maps tuned frequencies to primary-source allocations, and computes dial positions.

from __future__ import annotations

import math
from dataclasses import dataclass

from .lessons import LessonError
from .operate import INSTRUMENTS, instrument
from .spectrum import DEFAULT_HIGH, DEFAULT_LOW, TUNER_MAX_HZ, TUNER_MIN_HZ

CAPTION = "Where each lesson tunes. A pin never means a signal on the air now."
KEY_LINE = "US table key: CAPITALS = primary service; lowercase = secondary."

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

    `channel_bands` and `between_hz` cover paired narrow channels such as US52's AIS 1 and AIS 2,
    where the course tunes the gap frequency between the two bands so one receiver hears both.
    """

    low_hz: int
    high_hz: int
    heading: str
    service: str
    cite: str
    edge_hz: int | None = None
    channel_bands: tuple[tuple[int, int], ...] = ()
    between_hz: int | None = None

    def contains_hz(self, hz: int) -> bool:
        if self.edge_hz is not None:
            return hz == self.edge_hz
        if self.channel_bands:
            return self.between_hz == hz or any(lo <= hz < hi for lo, hi in self.channel_bands)
        return self.low_hz <= hz < self.high_hz

    def contains_band(self, low_hz: int, high_hz: int) -> bool:
        if self.edge_hz is not None or self.channel_bands:
            return False
        return self.low_hz <= low_hz and high_hz <= self.high_hz


# Primary-source allocations only:
# - Table rows: NTIA Redbook Table of Frequency Allocations (redbook.ntia.gov, read 2026-11-28).
# - Footnotes: 47 CFR 2.106 on eCFR, up to date as of 2026-11-25 (read 2026-11-28).
# Per the US table convention, ALL CAPS marks primary services and lowercase marks secondary.
ALLOCATIONS: tuple[Allocation, ...] = (
    Allocation(
        low_hz=88_000_000,
        high_hz=108_000_000,
        heading="88-108 MHz",
        service="BROADCASTING (FM, non-Federal, primary)",
        cite=(
            "NTIA Table of Frequency Allocations, row 88-108 MHz (read 2026-11-28); "
            "47 CFR 2.106(d)(5) (NG5) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(c)(93) (US93) (eCFR as of 2026-11-25, read 2026-11-28)"
        ),
    ),
    Allocation(
        low_hz=117_975_000,
        high_hz=137_000_000,
        heading="117.975-137 MHz",
        service="AERONAUTICAL MOBILE (primary)",
        cite=(
            "NTIA Table of Frequency Allocations, rows 123.5875-128.8125 and 128.8125-132.0125 MHz "
            "(read 2026-11-28); "
            "47 CFR 2.106(b)(200) (5.200) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(c)(26) (US26) (eCFR as of 2026-11-25, read 2026-11-28)"
        ),
    ),
    Allocation(
        low_hz=137_000_000,
        high_hz=138_000_000,
        heading="137-138 MHz",
        service=(
            "space-to-Earth: METEOROLOGICAL-SATELLITE, SPACE OPERATION, SPACE RESEARCH; "
            "MOBILE-SATELLITE (primary at 137.5 MHz; mobile-satellite secondary on "
            "137.025-137.175 and 137.825-138 MHz)"
        ),
        cite=(
            "NTIA Table of Frequency Allocations, rows 137-137.025, 137.025-137.175, "
            "137.175-137.825, 137.825-138 MHz (read 2026-11-28); "
            "47 CFR 2.106(b)(208) (5.208) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(c)(319) (US319) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(c)(320) (US320) (eCFR as of 2026-11-25, read 2026-11-28)"
        ),
    ),
    Allocation(
        low_hz=144_000_000,
        high_hz=148_000_000,
        heading="144-148 MHz",
        service=(
            "AMATEUR (non-Federal, primary; AMATEUR-SATELLITE primary on 144-146 MHz; "
            "no Federal service)"
        ),
        cite=(
            "NTIA Table of Frequency Allocations, rows 144-146 and 146-148 MHz (read 2026-11-28)"
        ),
    ),
    Allocation(
        low_hz=161_962_500,
        high_hz=162_037_500,
        heading="AIS 1 (161.9625-161.9875 MHz) and AIS 2 (162.0125-162.0375 MHz)",
        service=(
            "MARITIME MOBILE, MOBILE-SATELLITE (Earth-to-space), "
            "AERONAUTICAL MOBILE (OR) — AIS (US52)"
        ),
        cite=(
            "47 CFR 2.106(c)(52) (US52) (eCFR as of 2026-11-25, read 2026-11-28); "
            "NTIA Table of Frequency Allocations, rows 161.9625-161.9875 and 162.0125-162.0375 MHz "
            "(read 2026-11-28); "
            "NTIA Table of Frequency Allocations, row 161.9875-162.0125 MHz (read 2026-11-28)"
        ),
        channel_bands=((161_962_500, 161_987_500), (162_012_500, 162_037_500)),
        between_hz=162_000_000,
    ),
    # 5.138 (433.05-434.79 MHz ISM) is Region 1 only and not printed in the US columns;
    # US269 governs non-Federal radiolocation, not the Federal primary allocation named here.
    Allocation(
        low_hz=420_000_000,
        high_hz=450_000_000,
        heading="420-450 MHz",
        service="RADIOLOCATION (Federal, primary); amateur (non-Federal, secondary)",
        cite=(
            "NTIA Table of Frequency Allocations, row 420-450 MHz (read 2026-11-28); "
            "47 CFR 2.106(c)(270) (US270) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(b)(282) (5.282) (eCFR as of 2026-11-25, read 2026-11-28)"
        ),
    ),
    Allocation(
        low_hz=450_000_000,
        high_hz=450_000_000,
        heading="450.0 MHz · edge of 420-450 and 450-454 MHz",
        service=(
            "420-450 MHz (Federal RADIOLOCATION, primary; non-Federal amateur, secondary) and "
            "450-454 MHz (non-Federal LAND MOBILE, primary; US87 space telecommand "
            "449.75-450.25 MHz)"
        ),
        cite=(
            "NTIA Table of Frequency Allocations, rows 420-450 and 450-454 MHz (read 2026-11-28); "
            "47 CFR 2.106(b)(286) (5.286) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(c)(87) (US87) (eCFR as of 2026-11-25, read 2026-11-28)"
        ),
        edge_hz=450_000_000,
    ),
    Allocation(
        low_hz=929_000_000,
        high_hz=930_000_000,
        heading="929-930 MHz",
        service=(
            "FIXED, LAND MOBILE (non-Federal, primary); "
            "Federal ship radiolocation offshore (US268)"
        ),
        cite=(
            "NTIA Table of Frequency Allocations, row 929-930 MHz (read 2026-11-28); "
            "47 CFR 2.106(c)(116) (US116) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(c)(268) (US268) (eCFR as of 2026-11-25, read 2026-11-28)"
        ),
    ),
    Allocation(
        low_hz=960_000_000,
        high_hz=1_164_000_000,
        heading="960-1164 MHz",
        service="AERONAUTICAL RADIONAVIGATION, AERONAUTICAL MOBILE (R) (both primary)",
        cite=(
            "NTIA Table of Frequency Allocations, row 960-1164 MHz (read 2026-11-28); "
            "47 CFR 2.106(b)(327)(i) (5.327A) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(b)(328) (5.328) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(c)(78) (US78) (eCFR as of 2026-11-25, read 2026-11-28); "
            "47 CFR 2.106(c)(224) (US224) (eCFR as of 2026-11-25, read 2026-11-28)"
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
    between_channels: bool = False
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
            fixed_hz = tuple(inst.fixed_hz) if inst else ()
            between = len(fixed_hz) == 2 and min(fixed_hz) < center < max(fixed_hz)
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
                    fixed_hz=fixed_hz,
                    between_channels=between,
                    status=status,
                    note=note,
                )
            )
            continue

        if "rtl-sdr" in lesson.hardware:
            inst = _instrument_for_lesson(lesson)
            fixed_hz = tuple(inst.fixed_hz) if inst else ()
            if fixed_hz:
                center = sum(fixed_hz) // len(fixed_hz)
                between = len(fixed_hz) == 2 and min(fixed_hz) < center < max(fixed_hz)
                dial_rows.append(
                    BandRow(
                        lesson_id=lesson.id,
                        title=lesson.title,
                        kind="tuned",
                        low_hz=center,
                        high_hz=center,
                        center_hz=center,
                        tool=inst.tool,
                        instrument_label=inst.label,
                        fixed_hz=fixed_hz,
                        between_channels=between,
                        status=status,
                        note=note,
                    )
                )
                continue
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
                    fixed_hz=fixed_hz,
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
