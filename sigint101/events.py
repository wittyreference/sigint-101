# ABOUTME: Append-only JSONL event log — the provenance substrate behind reception badges.
# ABOUTME: Every logged action appends one line; history is never rewritten, so a badge can cite it.

from __future__ import annotations

import json
import time
from pathlib import Path


def log_event(path, kind: str, payload: dict, *, clock=time.time) -> dict:
    """Append one event to the JSONL log and return it. Creates the file if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"ts": round(clock(), 3), "kind": str(kind), "payload": payload}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def read_events(path) -> list:
    """Read the whole log back, oldest first.

    A missing file means nothing has happened yet, which is not an error — the console
    logs its first event the first time it touches the radio.
    """
    path = Path(path)
    if not path.exists():
        return []
    events = []
    with open(path, "r", encoding="utf-8") as handle:
        # One JSON object per line, so a truncated write costs one event rather than the
        # whole history — the reason this is JSONL and not a single JSON array. The kiosk
        # loses power mid-write, so an unparseable line is expected damage: skip it and
        # keep the rest, which is the tolerance the format was chosen for.
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events
