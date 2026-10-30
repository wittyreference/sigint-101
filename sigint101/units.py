# ABOUTME: The curriculum map: the six-unit numbered spine (U1-U6) plus the U7 counter-
# ABOUTME: surveillance track, grouping lessons for the home nav; membership looked up by id.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    """One curriculum unit: a named group of lessons the home grid drills into.

    Membership is the plan's fixed module spine (U1 = M1-M4, and so on), so it lives here
    as curriculum rather than being declared per pack — a pack's id is enough to place it.
    `glyph` names the line icon the home tile draws (see home.html's glyph macro).
    `lesson_ids` is every *planned* lesson in the unit, in order; a unit shows only the ones
    whose packs have actually loaded, so unbuilt lessons simply do not appear yet (no lock, D16).
    """

    id: int
    slug: str
    title: str
    glyph: str
    lesson_ids: tuple


# The curriculum spine from the plan. Glyphs map 1:1 onto the home template's icon set.
# U7 is the counter-surveillance track (activated 2026-10-29); it is built first, so its
# lesson ids (17-19) are the lowest of the as-yet-unbuilt range even though it displays last.
UNITS: tuple = (
    Unit(1, "instrument", "Instrument", "waterfall", (1, 2, 3, 4)),
    Unit(2, "rules", "Rules", "rules", (5,)),
    Unit(3, "antennas", "Antennas", "antenna", (6, 7)),
    Unit(4, "modulation", "Modulation", "wave", (8, 9, 10, 11, 12)),
    Unit(5, "decode", "Decode", "decode", (13, 14, 15, 16, 26, 27)),
    Unit(6, "under-the-hood", "Under the Hood", "gear", (20, 21, 22, 23)),
    Unit(7, "counter-surveillance", "Counter-Surveillance", "counter-surv", (17, 18, 19)),
    Unit(8, "images", "Images", "images", (24, 25)),
    Unit(9, "mesh", "Mesh", "mesh", (28,)),
)


def get_unit(unit_id: int) -> Unit | None:
    """The unit with this id, or None if there is no such unit."""
    for unit in UNITS:
        if unit.id == unit_id:
            return unit
    return None


def unit_for_lesson(lesson_id: int) -> Unit | None:
    """The unit a lesson id belongs to, or None if it falls outside the curriculum map."""
    for unit in UNITS:
        if lesson_id in unit.lesson_ids:
            return unit
    return None
