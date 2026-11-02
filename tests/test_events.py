# ABOUTME: Unit tests for the append-only event log.
# ABOUTME: Events accumulate in order and history is never rewritten.

from sigint101.events import EventFollower, log_event, read_events


def test_log_and_read_roundtrip(tmp_path):
    path = tmp_path / "sub" / "events.jsonl"
    event = log_event(path, "reception", {"freq_hz": 433_920_000}, clock=lambda: 100.0)
    assert event["kind"] == "reception"
    assert event["ts"] == 100.0
    events = read_events(path)
    assert len(events) == 1
    assert events[0]["payload"] == {"freq_hz": 433_920_000}


def test_append_only(tmp_path):
    path = tmp_path / "events.jsonl"
    log_event(path, "a", {}, clock=lambda: 1.0)
    log_event(path, "b", {}, clock=lambda: 2.0)
    assert [e["kind"] for e in read_events(path)] == ["a", "b"]


def test_read_missing_returns_empty(tmp_path):
    assert read_events(tmp_path / "nope.jsonl") == []


def test_a_truncated_line_costs_one_event_not_the_history(tmp_path):
    # The kiosk loses power mid-write; JSONL exists so that costs the one line.
    path = tmp_path / "events.jsonl"
    log_event(path, "a", {}, clock=lambda: 1.0)
    log_event(path, "b", {}, clock=lambda: 2.0)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"ts": 3.0, "kind": "c", "pay')
    assert [e["kind"] for e in read_events(path)] == ["a", "b"]


def test_blank_lines_are_skipped(tmp_path):
    # A blank line is the same expected damage as a truncated one — skip it, keep the rest.
    path = tmp_path / "events.jsonl"
    log_event(path, "a", {}, clock=lambda: 1.0)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n")
    log_event(path, "b", {}, clock=lambda: 2.0)
    assert [e["kind"] for e in read_events(path)] == ["a", "b"]


# --- T-15: the log is capped by rotation, and followed rather than re-read --------------------


def test_a_full_log_rotates_and_the_oldest_generation_drops(tmp_path):
    path = tmp_path / "events.jsonl"
    for n in range(40):
        log_event(path, "tick", {"n": n}, clock=lambda: 1.0, max_bytes=200, keep=2)
    assert path.exists()
    assert (tmp_path / "events.jsonl.1").exists()
    assert (tmp_path / "events.jsonl.2").exists()
    assert not (tmp_path / "events.jsonl.3").exists()
    for name in ("events.jsonl", "events.jsonl.1", "events.jsonl.2"):
        assert (tmp_path / name).stat().st_size < 200 + 100


def test_reading_the_log_spans_its_rotated_generations_in_order(tmp_path):
    path = tmp_path / "events.jsonl"
    for n in range(12):
        log_event(path, "tick", {"n": n}, clock=lambda: 1.0, max_bytes=200, keep=5)
    ns = [e["payload"]["n"] for e in read_events(path)]
    assert ns == list(range(12))


def test_a_follower_parses_each_line_once(tmp_path):
    path = tmp_path / "events.jsonl"
    log_event(path, "a", {}, clock=lambda: 1.0)
    log_event(path, "b", {}, clock=lambda: 2.0)
    follower = EventFollower(path)
    assert [e["kind"] for e in follower.events()] == ["a", "b"]
    # Scribble over what was read: a follower that re-read the file would notice.
    size = path.stat().st_size
    path.write_bytes(b"x" * (size - 1) + b"\n")
    assert [e["kind"] for e in follower.events()] == ["a", "b"]


def test_a_follower_picks_up_appends_and_waits_for_a_whole_line(tmp_path):
    path = tmp_path / "events.jsonl"
    follower = EventFollower(path)
    assert follower.events() == []                # no log yet is nothing yet
    log_event(path, "a", {}, clock=lambda: 1.0)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"ts": 2.0, "kind": "b", "pay')   # a write still in flight
    assert [e["kind"] for e in follower.events()] == ["a"]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('load": {}}\n')
    assert [e["kind"] for e in follower.events()] == ["a", "b"]


def test_a_follower_keeps_its_place_across_a_rotation(tmp_path):
    path = tmp_path / "events.jsonl"
    follower = EventFollower(path, kind="keep")
    for n in range(30):
        log_event(path, "keep" if n % 3 == 0 else "skip", {"n": n}, clock=lambda: 1.0,
                  max_bytes=300, keep=20)
        follower.events()
    assert [e["payload"]["n"] for e in follower.events()] == list(range(0, 30, 3))


def test_a_follower_catches_lines_written_just_before_a_rotation(tmp_path):
    # Lines appended after the last poll and then rotated away still belong to the follower.
    path = tmp_path / "events.jsonl"
    follower = EventFollower(path)
    log_event(path, "a", {}, clock=lambda: 1.0, max_bytes=10_000, keep=3)
    follower.events()
    log_event(path, "b", {}, clock=lambda: 2.0, max_bytes=10_000, keep=3)
    log_event(path, "c", {}, clock=lambda: 3.0, max_bytes=1, keep=3)    # rotates b's file away
    assert [e["kind"] for e in follower.events()] == ["a", "b", "c"]


def test_a_new_follower_reads_the_rotated_generations_too(tmp_path):
    path = tmp_path / "events.jsonl"
    for n in range(12):
        log_event(path, "keep", {"n": n}, clock=lambda: 1.0, max_bytes=200, keep=10)
    assert [e["payload"]["n"] for e in EventFollower(path).events()] == list(range(12))
