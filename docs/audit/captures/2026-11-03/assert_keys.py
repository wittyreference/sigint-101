# ABOUTME: Checks the L1, L2 and L8 predict keys against real nomad-b waterfall rows (capture.py):
# ABOUTME: stripe width by brightness, the meter floor across the gain ladder, and airband gaps.
"""Tier-C key check (docs/audit/2026-11-03-item-review.md §1.4, §3).

Each check measures what the console's waterfall and meter draw from the committed rows, names the
chip that measurement supports, and compares it with the pack's `answer`. Exit status 1 when any
captured item disagrees with its pack. An item with no capture is reported as provisional.

    python docs/audit/captures/2026-11-03/assert_keys.py
"""

import csv
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]

# A bin counts as lit when it stands this far above its own row's median: the review's rule for
# "what a learner reads as part of a stripe" (§3, L1 tier C).
LIT_DB = 6.0

# US FM channels sit on odd tenths of a MHz, 200 kHz apart.
FM_FIRST_CHANNEL_HZ = 88_100_000
FM_SPACING_HZ = 200_000

# The L2 rewrite's threshold: the floor "rises" when the top rung reads at least this much above
# the middle rung (§3, L2 tier C).
FLOOR_MOVED_DB = 3.0


@dataclass(frozen=True)
class Stripe:
    start: int
    stop: int
    peak_index: int
    peak_db: float
    median_db: float
    cut: bool

    @property
    def width(self) -> int:
        return self.stop - self.start


@dataclass(frozen=True)
class Station:
    channel_hz: float
    width: float
    level_db: float
    presence: float


def read_rows(path) -> list:
    """Rows as capture.py wrote them: label, ts, gain, freq_low, freq_step, dB per bin."""
    rows = []
    with open(path, newline="") as fh:
        for rec in csv.reader(fh):
            gain = None if rec[2] in ("", "None") else float(rec[2])
            rows.append({"label": rec[0], "ts": rec[1], "gain": gain,
                         "freq_low": float(rec[3]), "freq_step": float(rec[4]),
                         "dbm": [None if v == "" else float(v) for v in rec[5:]]})
    return rows


def median_db(dbm) -> float:
    """The meter's floor: the median of the measured bins (SweepRow.noise_floor_db)."""
    return statistics.median(v for v in dbm if v is not None)


def stripes(dbm, above_db=LIT_DB) -> list:
    """Runs of bins at least `above_db` over the row's median.

    An unmeasured bin ends a run, and a run that touches one is marked cut: the waterfall paints
    that bin empty, splitting the stripe.
    """
    floor = median_db(dbm)
    found, start = [], None
    for i, v in enumerate(list(dbm) + [None]):
        lit = v is not None and v >= floor + above_db
        if lit and start is None:
            start = i
        elif not lit and start is not None:
            run = dbm[start:i]
            peak = max(run)
            cut = (start > 0 and dbm[start - 1] is None) or (i < len(dbm) and dbm[i] is None)
            found.append(Stripe(start, i, start + run.index(peak), peak, floor, cut))
            start = None
    return found


def fm_channel(hz) -> float:
    return FM_FIRST_CHANNEL_HZ + round((hz - FM_FIRST_CHANNEL_HZ) / FM_SPACING_HZ) * FM_SPACING_HZ


def fm_stations(rows, above_db=LIT_DB) -> list:
    """Each FM channel's stripe across the rows: its median width in bins, its median level above
    the floor, and the share of rows it was lit in. Cut stripes are left out of width and level."""
    seen = {}
    for row in rows:
        for s in stripes(row["dbm"], above_db):
            if s.cut:
                continue
            channel = fm_channel(row["freq_low"] + s.peak_index * row["freq_step"])
            entry = seen.setdefault(channel, {"widths": [], "levels": []})
            entry["widths"].append(s.width)
            entry["levels"].append(s.peak_db - s.median_db)
    return sorted((Station(ch, statistics.median(e["widths"]), statistics.median(e["levels"]),
                           len(e["widths"]) / len(rows)) for ch, e in seen.items()),
                  key=lambda s: s.channel_hz)


def width_pairs(stations, n=3, min_presence=0.8) -> list:
    """The n brightest steady stripes, each paired with its counterpart among the n dimmest."""
    steady = sorted((s for s in stations if s.presence >= min_presence),
                    key=lambda s: s.level_db, reverse=True)
    if len(steady) < 2 * n:
        return []
    return list(zip(steady[:n], reversed(steady[-n:])))


def width_verdict(pairs):
    """The L1 chip the pairs support, or None when they disagree with each other."""
    if not pairs:
        return None
    if all(d.width < b.width for b, d in pairs):
        return "narrower"
    if all(d.width == b.width for b, d in pairs):
        return "same"
    if all(d.width > b.width for b, d in pairs):
        return "wider"
    return None


def neighbour_widths(stations, min_presence=0.8, apart_db=LIT_DB) -> dict:
    """Side-by-side stripes, as the L1 observe has the learner compare them: for each steady
    station and the next steady one up the band, at least `apart_db` apart in level, whether the
    dimmer one drew narrower, the same width, or wider."""
    steady = [s for s in stations if s.presence >= min_presence]
    tally = {"narrower": 0, "same": 0, "wider": 0}
    for a, b in zip(steady, steady[1:]):
        if abs(a.level_db - b.level_db) < apart_db:
            continue
        bright, dim = (a, b) if a.level_db > b.level_db else (b, a)
        side = "narrower" if dim.width < bright.width else (
            "same" if dim.width == bright.width else "wider")
        tally[side] += 1
    return tally


def gain_runs(rows) -> list:
    """Per run: the meter's floor and peak at the middle and top rungs, and how far each moved."""
    phases = {}
    for row in rows:
        floor = median_db(row["dbm"])
        peak = max(v for v in row["dbm"] if v is not None)
        phases.setdefault(row["label"], []).append((floor, peak))
    runs = []
    for label in sorted(phases):
        if not label.endswith("-middle"):
            continue
        run = label[: -len("-middle")]
        mid, top = phases[label], phases.get(run + "-max")
        if not top:
            continue
        mid_floor = statistics.median(f for f, _ in mid)
        mid_peak = statistics.median(p for _, p in mid)
        top_floor = statistics.median(f for f, _ in top)
        top_peak = statistics.median(p for _, p in top)
        runs.append({"run": run, "rows": (len(mid), len(top)),
                     "middle_floor_db": mid_floor, "max_floor_db": top_floor,
                     "middle_peak_db": mid_peak, "max_peak_db": top_peak,
                     "floor_rise_db": top_floor - mid_floor, "peak_rise_db": top_peak - mid_peak,
                     "gap_change_db": (top_peak - top_floor) - (mid_peak - mid_floor)})
    return runs


def gain_verdict(runs, moved_db=FLOOR_MOVED_DB):
    """The L2 chip the runs support, or None when they disagree with each other."""
    if not runs:
        return None
    rises = [r["floor_rise_db"] for r in runs]
    if all(d >= moved_db for d in rises):
        return "rises"
    if all(abs(d) < moved_db for d in rises):
        return "stays"
    if all(d <= -moved_db for d in rises):
        return "sinks"
    return None


def _runs(flags):
    """Lengths of consecutive True and False stretches, in order, as (flag, length)."""
    out = []
    for f in flags:
        if out and out[-1][0] == f:
            out[-1][1] += 1
        else:
            out.append([f, 1])
    return [(f, n) for f, n in out]


def airband_channels(rows, above_db=LIT_DB) -> list:
    """Every bin that lights at least once: its lit stretches, and the dark gaps between them (a
    dark stretch counts as a gap only with a lit stretch on both sides), all in rows."""
    width = len(rows[0]["dbm"])
    lit = [[False] * len(rows) for _ in range(width)]
    for t, row in enumerate(rows):
        floor = median_db(row["dbm"])
        for b, v in enumerate(row["dbm"]):
            lit[b][t] = v is not None and v >= floor + above_db
    channels = []
    for b in range(width):
        if not any(lit[b]):
            continue
        stretches = _runs(lit[b])
        gaps = [n for i, (f, n) in enumerate(stretches)
                if not f and 0 < i < len(stretches) - 1]
        channels.append({"bin": b, "freq_hz": rows[0]["freq_low"] + b * rows[0]["freq_step"],
                         "presence": sum(lit[b]) / len(rows),
                         "lit_runs": [n for f, n in stretches if f], "gaps": gaps})
    return channels


def read_frames(path) -> list:
    """50 ms frames as iq_carrier.py wrote them."""
    with open(path, newline="") as fh:
        return [{"t": float(r[0]), "carrier_db": float(r[1]), "voice_db": float(r[2]),
                 "chan_lin": float(r[3]), "bins_lin": float(r[4])} for r in csv.reader(fh)]


def carrier_check(frames, keyed_db=10.0, pause_db=10.0, pause_frames=4, per_row=20) -> dict:
    """What the I/Q frames say about L8's dark rows.

    A frame is keyed while its carrier line stands `keyed_db` over the median bin. A speech pause
    is `pause_frames` or more frames, inside one keyed stretch, whose sidebands sit `pause_db`
    below that stretch's median: a word gap heard while the carrier was still up. A dropout is a
    carrier-off stretch shorter than a row with a keyed one either side. Rows are `per_row`
    frames (one second), lit as the waterfall lights them: the channel's 42 kHz bin 6 dB over the
    median bin.
    """
    keyed = [f["carrier_db"] >= keyed_db for f in frames]
    stretches, i = [], 0
    for flag, n in _runs(keyed):
        stretches.append((flag, i, n))
        i += n
    pauses = 0
    for flag, start, n in stretches:
        if not flag:
            continue
        voice = [f["voice_db"] for f in frames[start:start + n]]
        typical = statistics.median(voice)
        quiet = [v <= typical - pause_db for v in voice]
        pauses += sum(1 for q, m in _runs(quiet) if q and m >= pause_frames)
    dropouts = sum(1 for k, (flag, _, n) in enumerate(stretches)
                   if not flag and 0 < k < len(stretches) - 1 and n < per_row)
    rows_lit, dark_keyed = [], 0
    for start in range(0, len(frames) - per_row + 1, per_row):
        row = frames[start:start + per_row]
        chan = sum(f["chan_lin"] for f in row) / per_row
        median = sum(f["bins_lin"] for f in row) / per_row
        lit = chan >= median * 10 ** (LIT_DB / 10)
        rows_lit.append(lit)
        if not lit and sum(keyed[start:start + per_row]) >= per_row / 2:
            dark_keyed += 1
    return {"transmissions": sum(1 for flag, _, _ in stretches if flag),
            "speech_pauses": pauses, "carrier_dropouts": dropouts,
            "rows_lit": rows_lit, "dark_rows_while_keyed": dark_keyed}


def carrier_verdict(found):
    """"ptt" when the carrier was heard holding through a word gap, no row went dark while a
    transmitter was keyed, and some row did go dark between transmissions; else None."""
    if (found["speech_pauses"] >= 1 and found["dark_rows_while_keyed"] == 0
            and not all(found["rows_lit"])):
        return "ptt"
    return None


def _pack_answers() -> dict:
    sys.path.insert(0, str(REPO))
    from sigint101.lessons import load_lessons

    return {i: lesson.answer for i, lesson in load_lessons(REPO / "lesson_packs").items()}


def main() -> int:
    answers = _pack_answers()
    failed = False

    l1 = HERE / "l1-fm-rows.csv"
    if l1.exists():
        stations = fm_stations(read_rows(l1))
        pairs = width_pairs(stations)
        measured = width_verdict(pairs)
        for b, d in pairs:
            print(f"L1 pair: bright {b.channel_hz / 1e6:.1f} MHz +{b.level_db:.1f} dB "
                  f"{b.width:g} bins | dim {d.channel_hz / 1e6:.1f} MHz +{d.level_db:.1f} dB "
                  f"{d.width:g} bins")
        print(f"L1 neighbours (dimmer vs brighter, >= {LIT_DB:g} dB apart): "
              f"{neighbour_widths(stations)}")
        failed |= _report(1, measured, answers[1])
    else:
        print("L1: no capture — key provisional")

    l2 = HERE / "l2-gain-rows.csv"
    if l2.exists():
        runs = gain_runs(read_rows(l2))
        for r in runs:
            print(f"L2 {r['run']}: floor {r['middle_floor_db']:.1f} -> {r['max_floor_db']:.1f} "
                  f"(+{r['floor_rise_db']:.1f}), peak {r['middle_peak_db']:.1f} -> "
                  f"{r['max_peak_db']:.1f} (+{r['peak_rise_db']:.1f}), gap change "
                  f"{r['gap_change_db']:+.1f} dB, rows {r['rows']}")
        failed |= _report(2, gain_verdict(runs), answers[2])
    else:
        print("L2: no capture — key provisional")

    print("L3: needs an antenna-off capture (a physical step) — key provisional")

    l8 = HERE / "l8-airband-rows.csv"
    if l8.exists():
        rows = read_rows(l8)
        busy = [c for c in airband_channels(rows) if len(c["lit_runs"]) >= 2]
        gaps = sorted(g for c in busy for g in c["gaps"])
        print(f"L8: {len(rows)} rows; {len(busy)} bins keyed up more than once; "
              f"dark gaps (rows) {gaps}")
        for c in sorted(busy, key=lambda c: -len(c["lit_runs"]))[:6]:
            print(f"L8 {c['freq_hz'] / 1e6:.3f} MHz: lit {c['lit_runs']} gaps {c['gaps']}")
    else:
        print("L8: no waterfall capture")
    iq = HERE / "l8-iq-frames.csv"
    if iq.exists():
        found = carrier_check(read_frames(iq))
        lit = found["rows_lit"]
        print(f"L8 I/Q: {found['transmissions']} keyed stretches, {found['speech_pauses']} word "
              f"gaps under a held carrier, {found['carrier_dropouts']} sub-second dropouts, "
              f"{lit.count(False)}/{len(lit)} rows dark, {found['dark_rows_while_keyed']} of "
              f"them while keyed")
        failed |= _report(8, carrier_verdict(found), answers[8])
    else:
        print("L8: no I/Q capture — key provisional")
    return 1 if failed else 0


def _report(lesson_id, measured, answer) -> bool:
    agrees = measured is not None and (measured,) == tuple(answer)
    print(f"L{lesson_id}: capture says {measured!r}, pack answers {list(answer)} -> "
          f"{'AGREES' if agrees else 'DISAGREES'}")
    return not agrees


if __name__ == "__main__":
    sys.exit(main())
