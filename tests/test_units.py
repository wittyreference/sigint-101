# ABOUTME: Tests the six-unit curriculum map (U1-U6) and its lookups, plus the contract
# ABOUTME: that every shipped lesson pack belongs to exactly one unit.

import re
from pathlib import Path

import yaml

from sigint101.lessons import load_lessons
from sigint101.units import (
    UNITS,
    get_unit,
    teaching_order,
    teaching_position,
    unit_for_lesson,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_units_are_the_spine_plus_the_extra_tracks():
    # Six numbered spine units (U1-U6), the counter-surveillance track (U7, 2026-10-29), the
    # Decode+ Images unit (U8, 2026-10-30), and the Meshtastic mesh mini-track (U9, 2026-10-30).
    assert [u.id for u in UNITS] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    # Names come from the plan's curriculum spine.
    assert get_unit(1).title == "Instrument"
    assert get_unit(6).slug == "under-the-hood"
    assert get_unit(7).slug == "counter-surveillance"
    assert get_unit(7).title == "Counter-Surveillance"
    assert get_unit(8).slug == "images"
    assert get_unit(8).title == "Images"
    assert get_unit(9).slug == "mesh"
    assert get_unit(9).title == "Mesh"


def test_units_partition_the_planned_lessons_without_overlap_or_gap():
    """Every placed lesson id lives in exactly one unit, with no duplicate across units.

    The numbered spine M1-M23 is fully placed; the Decode+ data/voice pair (M26 AIS, M27 DMR/P25)
    joins Unit 5 Decode, the Images pair (M24 APT, M25 SSTV) forms Unit 8, and Meshtastic (M28)
    forms the Unit 9 mesh mini-track — every lesson id 1..28 now lives in exactly one unit.
    """
    seen = []
    for u in UNITS:
        seen.extend(u.lesson_ids)
    assert sorted(seen) == list(range(1, 29))
    assert len(seen) == len(set(seen)), "a lesson id appears in two units"


def test_unit_for_lesson_maps_ids_to_their_unit():
    assert unit_for_lesson(1).id == 1    # M1 -> U1 Instrument
    assert unit_for_lesson(8).id == 4    # M8 -> U4 Modulation
    assert unit_for_lesson(17).id == 7   # M17 -> U7 counter-surveillance (built first)
    assert unit_for_lesson(18).id == 7   # M18 -> U7 counter-surveillance
    assert unit_for_lesson(20).id == 6   # M20 -> U6 Under the Hood
    assert unit_for_lesson(999) is None


def test_get_unit_rejects_unknown_ids():
    assert get_unit(99) is None
    assert get_unit(0) is None


def test_every_shipped_pack_belongs_to_a_unit(monkeypatch):
    """Guard: a new pack whose id falls outside the unit map would orphan it on the home grid."""
    monkeypatch.chdir(REPO_ROOT)
    lessons = load_lessons("lesson_packs")
    assert lessons, "expected at least one shipped pack"
    for lesson_id in lessons:
        assert unit_for_lesson(lesson_id) is not None, \
            f"lesson {lesson_id} is in no unit — add it to sigint101/units.py UNITS"


def test_teaching_order_walks_the_units_in_display_order():
    """The order a learner meets lessons: unit by unit as the home grid shows them.

    Ids were handed out as lessons were built, so they drift from that order once a unit holds a
    later-built lesson (AIS and DMR close Unit 5; counter-surveillance displays after Unit 6).
    """
    order = teaching_order()
    assert order[:16] == tuple(range(1, 17))
    assert order[16:18] == (26, 27)           # Decode+ pair closes Unit 5
    assert order[18:22] == (20, 21, 22, 23)   # Unit 6 Under the Hood
    assert order[22:25] == (17, 18, 19)       # Unit 7 counter-surveillance
    assert order[25:] == (24, 25, 28)         # Unit 8 Images, Unit 9 Mesh


def test_teaching_position_is_where_the_lesson_falls_in_that_order():
    assert teaching_position(1) == 1
    assert teaching_position(16) == 16
    assert teaching_position(26) == 17        # AIS: id 26, the 17th lesson taught
    assert teaching_position(17) == 23        # counter-surveillance framing
    assert teaching_position(28) == 28
    assert teaching_position(999) is None     # not in the curriculum map


# A cross-reference in pack copy: "Back in Lesson 13 the aircraft told you where…".
LESSON_REF = re.compile(r"\bLesson (\d+)\b")


def _copy_strings(node):
    """Every string a pack carries — concept, stage bodies, choices, hints — wherever it nests."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _copy_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _copy_strings(value)


def test_every_lesson_n_in_pack_copy_is_the_lesson_shown_as_ln():
    """A "Lesson N" in copy must name the lesson whose tile reads LN.

    Tiles show the teaching-order position (LN = the Nth lesson taught), while pack authors number
    a cross-reference by lesson id. The two agree only when the id-N lesson sits at position N, so
    this holds every reference to that: a "Lesson 17" written for id 17 (shown as L23) fails here
    instead of sending the learner to the wrong tile.
    """
    order = teaching_order()
    wrong = []
    for path in sorted((REPO_ROOT / "lesson_packs").glob("*.yaml")):
        pack = yaml.safe_load(path.read_text(encoding="utf-8"))
        for text in _copy_strings(pack):
            for match in LESSON_REF.finditer(text):
                n = int(match.group(1))
                if n > len(order) or order[n - 1] != n:
                    wrong.append(f"{path.name}: 'Lesson {n}'")
    assert not wrong, f"copy names a lesson by id where the tile shows another number: {wrong}"
