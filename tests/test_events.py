# ABOUTME: Unit tests for the append-only event log.
# ABOUTME: Events accumulate in order and history is never rewritten.

from sigint101.events import log_event, read_events


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
