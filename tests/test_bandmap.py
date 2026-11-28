# ABOUTME: Unit tests for the course band map data model, allocation citations, and dial scale.
# ABOUTME: Covers real packs, synthetic packs, band_map validation, 450.0 MHz edge, and tick math.

import re
from pathlib import Path

import pytest
import yaml

import sigint101.bandmap as bandmap_mod
import sigint101.lessons as lessons_mod
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

ECFR_CITE_RE = re.compile(
    r"^47 CFR 2\.106\((b|c|d|e)\)\(\d+\)(?:\([ivx]+\))? "
    r"\((?:5\.\d+[A-Z]?|US\d+|NG\d+|G\d+)\) "
    r"\(eCFR as of \d{4}-\d{2}-\d{2}, read \d{4}-\d{2}-\d{2}\)$"
)
NTIA_ROW_CITE_RE = re.compile(
    r"^NTIA Table of Frequency Allocations, rows? "
    r"[0-9., -]+(?: and [0-9.]+-[0-9.]+)? MHz \(read \d{4}-\d{2}-\d{2}\)$"
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
            expected = (
                lesson.tune["center_hz"]
                if lesson.tune is not None
                else sum(row.fixed_hz) // len(row.fixed_hz)
            )
            assert row.center_hz == expected
            assert row.low_hz == expected
            assert row.high_hz == expected
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

    # M24 is replay-only and says no APT recording ships on this kit yet; no other row has a status.
    assert by_id[24].status == "replay-only"
    assert by_id[24].note == "No APT recording ships on this kit yet"
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

    # RADIO_NOTE and KNOWN_HARDWARE stay aligned so load-time validation covers every off-dial kit.
    assert set(RADIO_NOTE) | {"rtl-sdr"} == set(getattr(lessons_mod, "KNOWN_HARDWARE", ()))

    # F7: unknown kit names fail at load_lessons, never only later at /bands.
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    _write_pack(bad_dir, "04-unknown.yaml", _minimal_pack(id=4, slug="unk", hardware=["mystery"]))
    with pytest.raises(LessonError, match="mystery"):
        load_lessons(bad_dir)


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
                "note": "No APT recording ships on this kit yet",
            }
        )
    )
    assert lesson.band_map == {
        "status": "replay-only",
        "note": "No APT recording ships on this kit yet",
    }


def test_allocations_edges_450_edge_heading_and_primary_citations():
    lessons = load_lessons(PACK_DIR)
    band_map = build_map(lessons)

    # N5: key line explains CAPITALS = primary, lowercase = secondary; headings write "MHz".
    key_line = getattr(bandmap_mod, "KEY_LINE", "")
    assert "CAPITALS" in key_line and "primary" in key_line and "secondary" in key_line
    for alloc in ALLOCATIONS:
        assert "MHZ" not in alloc.heading
        assert "MHz" in alloc.heading

    # Every row lies inside its heading's edges (or between US52's AIS 1 and AIS 2 bands for M26);
    # M27 sits under the 450.0 MHz edge heading.
    edge_groups = [g for g in band_map.groups if g.allocation.edge_hz == 450_000_000]
    assert len(edge_groups) == 1
    edge_group = edge_groups[0]
    assert [r.lesson_id for r in edge_group.rows] == [27]
    assert "420-450" in edge_group.allocation.heading
    assert "450-454 MHz" in edge_group.allocation.heading
    # F4: 450.0 MHz edge keeps primary/secondary and never says "no Federal service" for 450-454.
    edge_svc = edge_group.allocation.service
    assert "RADIOLOCATION" in edge_svc and "primary" in edge_svc
    assert "amateur" in edge_svc and "secondary" in edge_svc
    assert "LAND MOBILE" in edge_svc
    assert "US87" in edge_svc
    assert "no Federal service" not in edge_svc

    # No other heading contains 450.0 MHz.
    for alloc in ALLOCATIONS:
        if alloc.edge_hz == 450_000_000:
            assert alloc.contains_hz(450_000_000)
        else:
            assert not alloc.contains_hz(450_000_000), alloc

    group_by_lesson = {r.lesson_id: g for g in band_map.groups for r in g.rows}

    # F1: 929-930 MHz names US268 Federal ship radiolocation offshore, not "no Federal service".
    alloc_929 = group_by_lesson[15].allocation
    assert "no Federal service" not in alloc_929.service
    assert "FIXED" in alloc_929.service and "LAND MOBILE" in alloc_929.service
    assert "Federal ship radiolocation" in alloc_929.service and "US268" in alloc_929.service

    # F2: AIS uses US52's two channel edges (never invented 161.975-162.025 edges); M26 tunes
    # 162.0 MHz between them while rtl_ais listens on both channel centres inside those bands.
    ais_group = group_by_lesson[26]
    alloc_ais = ais_group.allocation
    assert "161.9625-161.9875 MHz" in alloc_ais.heading
    assert "162.0125-162.0375 MHz" in alloc_ais.heading
    assert "161.975 / 162.025" not in alloc_ais.heading
    assert alloc_ais.channel_bands == (
        (161_962_500, 161_987_500),
        (162_012_500, 162_037_500),
    )
    m26_row = next(r for r in ais_group.rows if r.lesson_id == 26)
    assert alloc_ais.channel_bands[0][1] < m26_row.center_hz < alloc_ais.channel_bands[1][0]
    for fixed in m26_row.fixed_hz:
        assert any(lo <= fixed < hi for lo, hi in alloc_ais.channel_bands)

    # F3: 137-138 MHz keeps space-to-Earth and primary/secondary mobile-satellite split.
    alloc_137 = group_by_lesson[24].allocation
    assert "space-to-Earth" in alloc_137.service
    assert "METEOROLOGICAL-SATELLITE" in alloc_137.service
    assert "SPACE OPERATION" in alloc_137.service
    assert "SPACE RESEARCH" in alloc_137.service
    assert "MOBILE-SATELLITE" in alloc_137.service
    assert "mobile-satellite secondary" in alloc_137.service
    assert "137.025-137.175" in alloc_137.service and "137.825-138" in alloc_137.service

    # F5: 420-450 MHz cites its own table row, never US269 or 5.138.
    alloc_420 = group_by_lesson[9].allocation
    assert "row 420-450 MHz" in alloc_420.cite
    assert "US269" not in alloc_420.cite
    assert "5.138" not in alloc_420.cite
    assert "RADIOLOCATION" in alloc_420.service and "amateur" in alloc_420.service

    # N3: 1090 MHz sits in 960-1164 MHz and names both primary services.
    alloc_1090 = group_by_lesson[13].allocation
    assert alloc_1090.heading == "960-1164 MHz"
    assert (alloc_1090.low_hz, alloc_1090.high_hz) == (960_000_000, 1_164_000_000)
    assert "AERONAUTICAL RADIONAVIGATION" in alloc_1090.service
    assert "AERONAUTICAL MOBILE (R)" in alloc_1090.service

    for group in band_map.groups:
        alloc = group.allocation
        for row in group.rows:
            if row.lesson_id == 27:
                assert alloc.edge_hz == 450_000_000
                assert row.center_hz == 450_000_000
            elif row.lesson_id == 26:
                assert alloc.between_hz == 162_000_000
                assert row.center_hz == 162_000_000
            elif row.kind == "tuned":
                assert alloc.contains_hz(row.center_hz), (row, alloc)
            else:
                assert alloc.contains_band(row.low_hz, row.high_hz), (row, alloc)

    # N4 + N6: every semicolon-separated part of every citation matches a dated primary source.
    for alloc in ALLOCATIONS:
        if alloc.service:
            assert alloc.cite, alloc
            parts = [part.strip() for part in alloc.cite.split(";")]
            assert parts, alloc
            for part in parts:
                assert ECFR_CITE_RE.match(part) or NTIA_ROW_CITE_RE.match(part), (
                    f"uncited or non-conforming citation part: {part!r} in {alloc.cite!r}"
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
