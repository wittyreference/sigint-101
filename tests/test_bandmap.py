# ABOUTME: Unit tests for the course band map data model, allocation citations, and dial scale.
# ABOUTME: Covers real packs, synthetic packs, band_map validation, 450.0 MHz edge, and tick math.

import re
from pathlib import Path

import pytest
import yaml

from sigint101.bandmap import (
    ALLOCATIONS,
    RADIO_NOTE,
    build_map,
    tick_x,
)
from sigint101.lessons import LessonError, load_lessons, parse_lesson
from sigint101.operate import INSTRUMENTS
from sigint101.spectrum import DEFAULT_HIGH, DEFAULT_LOW

PACK_DIR = Path(__file__).resolve().parent.parent / "lesson_packs"

CFR_CITE_RE = re.compile(r"^47 CFR 2\.106\((b|c|d|e)\)\(\d+\)")
FCC_ROW_CITE_RE = re.compile(
    r"^FCC Table of Frequency Allocations, row [0-9.]+-[0-9.]+ MHz \(rev\. \d{4}-\d{2}-\d{2}\)"
)


def _minimal_pack(**overrides) -> dict:
    pack = {
        "id": 99,
        "slug": "synthetic-test",
        "title": "Synthetic Test",
        "concept": "test concept",
        "hardware": ["rtl-sdr"],
        "stages": {
            "predict": "What happens?",
            "operate": "Run it.",
            "observe": "Watch it.",
            "explain": "Because physics.",
        },
    }
    pack.update(overrides)
    return pack


def _write_pack(tmp_path: Path, filename: str, data: dict) -> Path:
    path = tmp_path / filename
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_real_packs_partition_cleanly_into_the_four_classes():
    lessons = load_lessons(PACK_DIR)
    band_map = build_map(lessons)

    tuned_ids = {row.lesson_id for row in band_map.dial_rows if row.kind == "tuned"}
    default_ids = {row.lesson_id for row in band_map.dial_rows if row.kind == "default_sweep"}
    off_dial_ids = {row.lesson_id for row in band_map.off_dial}
    no_radio_ids = set(band_map.no_radio)

    assert tuned_ids == {6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 24, 25, 26, 27}
    assert default_ids == {1, 2, 3, 4}
    assert off_dial_ids == {11, 18, 19, 28, 29, 39}
    assert no_radio_ids == {5, 17, 20, 21, 22, 23}

    # Pairwise disjoint and covering every loaded lesson id.
    all_sets = [tuned_ids, default_ids, off_dial_ids, no_radio_ids]
    assert sum(len(s) for s in all_sets) == len(lessons)
    assert set().union(*all_sets) == set(lessons)
    assert band_map.no_radio_line == "No radio in: M5, M17, M20-M23"


def test_real_packs_frequencies_separate_rows_sorting_and_tools():
    lessons = load_lessons(PACK_DIR)
    band_map = build_map(lessons)

    by_id = {row.lesson_id: row for row in band_map.dial_rows}
    for row in band_map.dial_rows:
        lesson = lessons[row.lesson_id]
        if row.kind == "tuned":
            assert row.center_hz == lesson.tune["center_hz"]
            assert row.low_hz == lesson.tune["center_hz"]
            assert row.high_hz == lesson.tune["center_hz"]
        else:
            assert row.kind == "default_sweep"
            assert (row.low_hz, row.high_hz) == (DEFAULT_LOW, DEFAULT_HIGH)
            assert row.center_hz is None

    # Shared frequencies stay separate rows (never merged).
    assert [r.lesson_id for r in band_map.dial_rows if r.center_hz == 433_920_000] == [9, 10]
    assert [r.lesson_id for r in band_map.dial_rows if r.center_hz == 1_090_000_000] == [6, 7, 13]

    # Sorted by (low_hz, lesson_id).
    keys = [(row.low_hz, row.lesson_id) for row in band_map.dial_rows]
    assert keys == sorted(keys)

    # Meteor-M LRPT gets no row and no tick until C20 lands.
    assert all(row.center_hz != 137_900_000 for row in band_map.dial_rows)
    assert all(tick.hz != 137_900_000 for tick in band_map.ticks)

    # M24 is replay-only and no other row has a status.
    assert by_id[24].status == "replay-only"
    assert by_id[24].note == "NOAA APT off the air since August 2025"
    for row in (*band_map.dial_rows, *band_map.off_dial):
        if row.lesson_id != 24:
            assert row.status is None

    # Fixed frequencies come from operate.INSTRUMENTS.
    inst_by_key = {inst.key: inst for inst in INSTRUMENTS}
    assert by_id[26].fixed_hz == inst_by_key["ais"].fixed_hz == (161_975_000, 162_025_000)
    assert by_id[26].tool == inst_by_key["ais"].tool == "rtl_ais"
    assert by_id[13].fixed_hz == inst_by_key["adsb"].fixed_hz == (1_090_000_000,)
    assert by_id[13].tool == inst_by_key["adsb"].tool == "dump1090"
    assert by_id[10].tool == inst_by_key["ism"].tool == "rtl_433"
    assert by_id[1].tool == inst_by_key["waterfall"].tool == "rtl_power"


def test_synthetic_packs_classification_and_unknown_hardware_refusal(tmp_path):
    _write_pack(tmp_path, "01-fm.yaml", _minimal_pack(id=1, slug="fm", hardware=["rtl-sdr"]))
    _write_pack(tmp_path, "02-theory.yaml", _minimal_pack(id=2, slug="theory", hardware=[]))
    _write_pack(
        tmp_path,
        "03-mesh.yaml",
        _minimal_pack(id=3, slug="mesh", hardware=["lora"], panels=["lora"]),
    )
    lessons = load_lessons(tmp_path)
    band_map = build_map(lessons)

    assert len(band_map.dial_rows) == 1
    assert band_map.dial_rows[0].lesson_id == 1
    assert band_map.dial_rows[0].kind == "default_sweep"
    assert (band_map.dial_rows[0].low_hz, band_map.dial_rows[0].high_hz) == (
        DEFAULT_LOW,
        DEFAULT_HIGH,
    )

    assert band_map.no_radio == (2,)
    assert band_map.no_radio_line == "No radio in: M2"

    assert len(band_map.off_dial) == 1
    off = band_map.off_dial[0]
    assert off.lesson_id == 3
    assert off.center_hz is None
    assert off.low_hz is None
    assert off.high_hz is None
    assert off.note == RADIO_NOTE["lora"]

    # Unknown hardware not in RADIO_NOTE fails loud.
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    _write_pack(bad_dir, "04-unknown.yaml", _minimal_pack(id=4, slug="unk", hardware=["mystery"]))
    with pytest.raises(LessonError, match="mystery"):
        build_map(load_lessons(bad_dir))


@pytest.mark.parametrize(
    "bad_band_map",
    [
        {"status": "live", "note": "Live station"},
        {"status": "replay-only", "note": "x" * 45},
        {"status": "replay-only", "note": "line one\nline two"},
        {"status": "replay-only"},
        {"status": "replay-only", "note": ""},
        {"note": "Missing status"},
        "replay-only",
    ],
)
def test_band_map_field_validation_refuses_malformed_entries(bad_band_map):
    with pytest.raises(LessonError):
        parse_lesson(_minimal_pack(band_map=bad_band_map))


def test_band_map_field_accepts_valid_replay_only_entry():
    lesson = parse_lesson(
        _minimal_pack(
            band_map={
                "status": "replay-only",
                "note": "NOAA APT off the air since August 2025",
            }
        )
    )
    assert lesson.band_map == {
        "status": "replay-only",
        "note": "NOAA APT off the air since August 2025",
    }


def test_allocations_edges_450_edge_heading_and_primary_citations():
    lessons = load_lessons(PACK_DIR)
    band_map = build_map(lessons)

    # Every row lies inside its heading's edges; M27 sits under the 450.0 MHz edge heading.
    edge_groups = [g for g in band_map.groups if g.allocation.edge_hz == 450_000_000]
    assert len(edge_groups) == 1
    edge_group = edge_groups[0]
    assert [r.lesson_id for r in edge_group.rows] == [27]
    assert "420-450" in edge_group.allocation.heading
    assert "450-454 MHz" in edge_group.allocation.heading

    # No other heading contains 450.0 MHz.
    for alloc in ALLOCATIONS:
        if alloc.edge_hz == 450_000_000:
            assert alloc.contains_hz(450_000_000)
        else:
            assert not alloc.contains_hz(450_000_000), alloc

    for group in band_map.groups:
        alloc = group.allocation
        for row in group.rows:
            if row.lesson_id == 27:
                assert alloc.edge_hz == 450_000_000
                assert row.center_hz == 450_000_000
            elif row.kind == "tuned":
                assert alloc.contains_hz(row.center_hz), (row, alloc)
            else:
                assert alloc.contains_band(row.low_hz, row.high_hz), (row, alloc)

    # Every allocation with a service name has a primary-source citation matching one of the two
    # required forms; an entry with no cite has no service name.
    for alloc in ALLOCATIONS:
        if alloc.service:
            assert alloc.cite, alloc
            assert CFR_CITE_RE.match(alloc.cite) or FCC_ROW_CITE_RE.match(alloc.cite), (
                f"uncited or non-conforming citation: {alloc.cite!r}"
            )
        if not alloc.cite:
            assert not alloc.service


def test_overview_dial_positions_and_replay_tick():
    assert tick_x(433_920_000) == pytest.approx(532.5, abs=0.1)
    assert tick_x(1_090_000_000) == pytest.approx(696.2, abs=0.1)

    lessons = load_lessons(PACK_DIR)
    band_map = build_map(lessons)
    assert band_map.fm_bar is not None
    assert band_map.fm_bar.x_start == pytest.approx(248.9, abs=0.1)
    assert band_map.fm_bar.x_end == pytest.approx(285.3, abs=0.1)

    ticks_by_hz = {tick.hz: tick for tick in band_map.ticks}
    assert ticks_by_hz[137_500_000].replay_only is True
    for hz, tick in ticks_by_hz.items():
        if hz != 137_500_000:
            assert tick.replay_only is False
