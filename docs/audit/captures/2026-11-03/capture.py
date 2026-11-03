# ABOUTME: Records real waterfall rows from a running SIGINT 101 console's own /api/spectrum, the
# ABOUTME: rows its waterfall paints, to CSV: the tier-C captures behind the L1, L2, L3 and L8 keys.
"""Tier-C capture for the predict items (docs/audit/2026-11-03-item-review.md §1.4, §3).

Run it against a console on the real radio (nomad-b), never against anything that invents rows:
it only records what the console's rtl_power sweep (spectrum.py) delivered.

    capture.py BASE fm OUT.csv [--seconds 60]           L1: the default FM sweep, auto gain
    capture.py BASE gain OUT.csv [--runs 3]             L2: middle vs top rung of the gain ladder
    capture.py BASE airband OUT.csv [--seconds 300]     L8: Lesson 8's 118-137 MHz band
    capture.py BASE span OUT.csv --low HZ --high HZ --label TEXT [--seconds 60]
                                                        L3: any span, labelled (antenna on / off)

Every row is written as: label, ts, gain, freq_low, freq_step, then one dB value per bin, with an
empty field where rtl_power measured nothing (a hop's centre bin).
"""

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
WATERFALL_JS = REPO / "sigint101" / "static" / "waterfall.js"

# Lesson 8's band as its page asks for it (parseTuneBand on the pack's tune block).
AIRBAND = {"low": 118_000_000, "high": 137_000_000, "bin_size": 50_000}
FM = {"low": 88_000_000, "high": 108_000_000, "bin_size": 125_000}


def gain_ladder() -> list:
    """The gain stepper's own rungs, read from the console's waterfall.js."""
    match = re.search(r"GAIN_STEPS\s*=\s*\[([^\]]*)\]", WATERFALL_JS.read_text())
    return [float(x) for x in match.group(1).split(",")]


def call(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def collect(base, seconds, label, writer, skip=0, seen=None):
    """Write every new row the console publishes for `seconds`, after dropping the first `skip`."""
    seen = set() if seen is None else seen
    end = time.time() + seconds
    kept = 0
    while time.time() < end:
        body = call(base, "/api/spectrum?limit=5")
        if not body.get("available", False):
            raise SystemExit(f"no sweep: {body.get('reason', body)}")
        for row in body["rows"]:
            mark = (row["ts"], row["freq_low"], tuple(row["dbm"][:8]))
            if mark in seen:
                continue
            seen.add(mark)
            if skip:
                skip -= 1
                continue
            writer.writerow([label, row["ts"], body["gain"], row["freq_low"], row["freq_step"]]
                            + ["" if v is None else f"{v:.2f}" for v in row["dbm"]])
            kept += 1
        time.sleep(0.4)
    return kept


def snapshot(base) -> set:
    """Marks of the rows already buffered, so only rows taken after a change are recorded."""
    rows = call(base, "/api/spectrum")["rows"]
    return {(r["ts"], r["freq_low"], tuple(r["dbm"][:8])) for r in rows}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("mode", choices=["fm", "gain", "airband", "span"])
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--low", type=int)
    ap.add_argument("--high", type=int)
    ap.add_argument("--bin", type=int, default=125_000)
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")

    with open(args.out, "w", newline="") as fh:
        writer = csv.writer(fh)
        if args.mode == "gain":
            ladder = gain_ladder()
            middle, top = ladder[len(ladder) // 2], ladder[-1]
            call(base, "/api/tune", FM)
            call(base, "/api/spectrum")
            for run in range(1, args.runs + 1):
                for name, gain in (("middle", middle), ("max", top)):
                    call(base, "/api/gain", {"gain": gain})
                    seen = snapshot(base)
                    n = collect(base, args.seconds, f"run{run}-{name}", writer, skip=2, seen=seen)
                    print(f"run {run} {name} {gain} dB: {n} rows", file=sys.stderr)
            call(base, "/api/gain", {"gain": None})
            return
        if args.mode == "fm":
            call(base, "/api/gain", {"gain": None})
            call(base, "/api/tune", FM)
            label = "fm-auto"
        elif args.mode == "airband":
            call(base, "/api/gain", {"gain": None})
            call(base, "/api/tune", AIRBAND)
            label = "airband-auto"
        else:
            call(base, "/api/tune", {"low": args.low, "high": args.high, "bin_size": args.bin})
            label = args.label
        call(base, "/api/spectrum")
        n = collect(base, args.seconds, label, writer, skip=2, seen=snapshot(base))
        print(f"{label}: {n} rows", file=sys.stderr)


if __name__ == "__main__":
    main()
