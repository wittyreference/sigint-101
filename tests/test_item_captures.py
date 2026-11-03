# ABOUTME: Tests the tier-C key check over real nomad-b waterfall captures: how it measures stripe
# ABOUTME: width, the gain floor and airband gaps, and that each capture backs its pack's answer.

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "docs" / "audit" / "captures" / "2026-11-03" / "assert_keys.py"


def _load():
    spec = importlib.util.spec_from_file_location("assert_keys", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


keys = _load()


def _row(dbm, label="x", gain=None, low=88_000_000.0, step=100_000.0):
    return {"label": label, "ts": "t", "gain": gain, "freq_low": low, "freq_step": step,
            "dbm": list(dbm)}


def test_a_stripe_is_a_run_of_bins_well_above_the_rows_median():
    # Median is 0; the 6 dB rule lights bins 2-4 (one stripe, 3 wide) and bin 8 (1 wide).
    dbm = [0, 0, 7, 20, 9, 0, 0, 0, 6, 0, 0]
    found = keys.stripes(dbm)
    assert [(s.start, s.stop) for s in found] == [(2, 5), (8, 9)]
    assert found[0].peak_index == 3 and found[0].width == 3


def test_an_unmeasured_bin_cuts_a_stripe_and_marks_it():
    # rtl_power's hop-centre bin is None: the waterfall paints it empty, so the stripe is split
    # there, and neither half is a fair width.
    dbm = [0, 0, 9, 10, None, 12, 0, 0, 0]
    found = keys.stripes(dbm)
    assert [(s.start, s.stop, s.cut) for s in found] == [(2, 4, True), (5, 6, True)]


def test_stations_are_named_by_the_fm_channel_their_peak_lands_on():
    # 100 kHz bins from 88.0 MHz: a peak in bin 11 is 89.1 MHz, an FM channel; bin 21 is 90.1.
    dbm = [0.0] * 30
    dbm[10:13] = [8, 20, 8]
    dbm[21] = 7
    rows = [_row(dbm) for _ in range(5)]
    stations = keys.fm_stations(rows)
    by_channel = {round(s.channel_hz): s for s in stations}
    assert by_channel[89_100_000].width == 3 and by_channel[89_100_000].level_db == 20
    assert by_channel[90_100_000].width == 1
    assert by_channel[89_100_000].presence == 1.0


def test_the_width_verdict_needs_every_dim_stripe_narrower_than_its_bright_partner():
    bright = [keys.Station(1, 5, 30, 1.0), keys.Station(2, 4, 25, 1.0), keys.Station(3, 4, 20, 1.0)]
    dim = [keys.Station(4, 1, 7, 1.0), keys.Station(5, 2, 8, 1.0), keys.Station(6, 2, 9, 1.0)]
    pairs = keys.width_pairs(bright + dim, n=3)
    assert [(b.channel_hz, d.channel_hz) for b, d in pairs] == [(1, 4), (2, 5), (3, 6)]
    assert keys.width_verdict(pairs) == "narrower"
    assert keys.width_verdict([(bright[0], keys.Station(9, 5, 7, 1.0))]) == "same"
    assert keys.width_verdict(pairs[:2] + [(bright[2], keys.Station(9, 6, 7, 1.0))]) is None


def test_the_gain_verdict_reads_the_meter_floor_at_middle_and_top_rungs():
    rows = []
    for run, (mid, top) in enumerate([(-30, -20), (-31, -22), (-30, -24)], start=1):
        rows += [_row([mid] * 9 + [mid + 25], label=f"run{run}-middle") for _ in range(3)]
        rows += [_row([top] * 9 + [top + 24], label=f"run{run}-max") for _ in range(3)]
    runs = keys.gain_runs(rows)
    assert [r["floor_rise_db"] for r in runs] == [10, 9, 6]
    assert [r["peak_rise_db"] for r in runs] == [9, 8, 5]
    assert keys.gain_verdict(runs) == "rises"
    flat = [dict(r, floor_rise_db=0.5) for r in runs]
    assert keys.gain_verdict(flat) == "stays"


def test_airband_gaps_are_dark_runs_between_lit_runs_on_one_channel():
    # Bin 3 is a channel that keys up for 3 rows, drops for 2, keys up for 1, then stays dark.
    lit = [1, 1, 1, 0, 0, 1, 0, 0]
    rows = [_row([0, 0, 0, 20 if on else 0, 0, 0, 0, 0]) for on in lit]
    channels = keys.airband_channels(rows)
    assert len(channels) == 1
    ch = channels[0]
    assert ch["bin"] == 3 and ch["lit_runs"] == [3, 1] and ch["gaps"] == [2]
