# ABOUTME: Tests the six-unit curriculum map (U1-U6) and its lookups, plus the contract
# ABOUTME: that every shipped lesson pack belongs to exactly one unit.

from pathlib import Path

from sigint101.lessons import load_lessons
from sigint101.units import UNITS, get_unit, unit_for_lesson

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
