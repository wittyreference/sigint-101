# ABOUTME: Append-only JSONL event log — the provenance substrate behind reception badges.
# ABOUTME: Every logged action appends one line; history is never rewritten, so a badge can cite it.

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

# The live log is rotated once it reaches this size: it becomes events.jsonl.1, the older
# generations shift up one, and anything beyond KEEP_ROTATED is dropped. Rotation moves whole
# files and never edits a line, so the log stays append-only; the cap keeps the SD card's share
# bounded (about 8 MB in all) however long the kiosk runs.
MAX_LOG_BYTES = 2_000_000
KEEP_ROTATED = 3

# Request threads log concurrently; rotating and appending happen as one step so no line lands
# in a file that is mid-rename.
_WRITE_LOCK = threading.Lock()


def _generation(path: Path, n: int) -> Path:
    return path.with_name(f"{path.name}.{n}")


def _rotated(path: Path) -> list:
    """The rotated generations that exist, oldest first."""
    found = []
    n = 1
    while _generation(path, n).exists():
        found.append(_generation(path, n))
        n += 1
    return list(reversed(found))


def _rotate_if_full(path: Path, max_bytes: int, keep: int) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < max_bytes:
        return
    for n in range(keep - 1, 0, -1):
        older = _generation(path, n)
        if older.exists():
            older.replace(_generation(path, n + 1))
    path.replace(_generation(path, 1))


def log_event(path, kind: str, payload: dict, *, clock=time.time,
              max_bytes=None, keep=None) -> dict:
    """Append one event to the JSONL log and return it. Creates the file if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"ts": round(clock(), 3), "kind": str(kind), "payload": payload}
    line = json.dumps(event, sort_keys=True) + "\n"
    with _WRITE_LOCK:
        _rotate_if_full(path, MAX_LOG_BYTES if max_bytes is None else max_bytes,
                        KEEP_ROTATED if keep is None else keep)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    return event


def _parse_lines(text: str, kind=None) -> list:
    # One JSON object per line, so a truncated write costs one event rather than the
    # whole history — the reason this is JSONL and not a single JSON array. The kiosk
    # loses power mid-write, so an unparseable line is expected damage: skip it and
    # keep the rest, which is the tolerance the format was chosen for.
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if kind is None or (isinstance(event, dict) and event.get("kind") == kind):
            events.append(event)
    return events


def read_events(path) -> list:
    """Read the whole log back, oldest first, its rotated generations included.

    A missing file means nothing has happened yet, which is not an error — the console
    logs its first event the first time it touches the radio.
    """
    path = Path(path)
    events = []
    for part in [*_rotated(path), path]:
        try:
            events.extend(_parse_lines(part.read_text(encoding="utf-8", errors="replace")))
        except FileNotFoundError:
            continue
    return events


class EventFollower:
    """Follows the event log as it grows, parsing each line once.

    The provenance panel asks for the baseline every 1.5 s. Re-reading the whole log for that cost
    1.3 s of CPU per poll at 100k lines; this remembers how far it has read and parses only what
    was appended since. A rotation is noticed by the live file's identity changing: the rest of
    the old file is read from the generation it was renamed to, then the new one from its start.
    Only whole lines are consumed, so a write still in flight is picked up on a later call.
    """

    def __init__(self, path, kind=None):
        self._path = Path(path)
        self._kind = kind
        self._lock = threading.Lock()
        self._events = []
        self._primed = False
        self._ino = None
        self._offset = 0

    def events(self) -> list:
        with self._lock:
            if not self._primed:
                self._primed = True
                for older in _rotated(self._path):
                    self._read_whole(older)
            self._follow()
            return list(self._events)

    def _read_whole(self, part: Path) -> None:
        try:
            text = part.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        self._events.extend(_parse_lines(text, self._kind))

    def _follow(self) -> None:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return
        if stat.st_ino != self._ino:
            if self._ino is not None:
                self._finish_rotated(self._ino)
            self._ino, self._offset = stat.st_ino, 0
        elif stat.st_size < self._offset:
            self._offset = 0          # truncated in place: start this file again
        self._offset = self._read_from(self._path, self._offset)

    def _finish_rotated(self, ino) -> None:
        """Read what was appended to the old live file after the last call, wherever it went."""
        for older in _rotated(self._path):
            try:
                if older.stat().st_ino == ino:
                    self._read_from(older, self._offset)
                    return
            except FileNotFoundError:
                continue

    def _read_from(self, part: Path, offset: int) -> int:
        try:
            with open(part, "rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except FileNotFoundError:
            return offset
        end = data.rfind(b"\n")
        if end < 0:
            return offset
        self._events.extend(_parse_lines(data[:end + 1].decode("utf-8", "replace"), self._kind))
        return offset + end + 1
