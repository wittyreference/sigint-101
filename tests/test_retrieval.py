# ABOUTME: Unit and route tests for pre-lesson retrieval cards (scheduler, queue, Skip, and
# ABOUTME: isolation from responses/stages/progress/exam.db, including the M5-M12 open-check case).

from __future__ import annotations

import ast
import re
import sqlite3
from html import unescape
from pathlib import Path

from fastapi.testclient import TestClient

from sigint101 import events as events_mod
from sigint101 import retrieval
from sigint101.app import create_app
from sigint101.events import read_events
from sigint101.hardware import RadioProbe
from sigint101.migrations import migrate_store_copy
from sigint101.progress import Progress

REPO = Path(__file__).resolve().parent.parent
DAY = 86_400


def _recall_client(data_dir: Path, pack_dir: Path, clock):
    app = create_app(
        pack_dir=pack_dir,
        data_dir=data_dir,
        probe=lambda: RadioProbe(present=False, reason="test"),
        clock=clock,
    )
    return TestClient(app)


def _write_pack(pack_dir: Path, lesson_id: int, *, with_check: bool = True,
                title: str | None = None) -> None:
    title = title or f"Lesson {lesson_id}"
    check_yaml = ""
    if with_check:
        check_yaml = f"""
check:
  prompt: "Check prompt for lesson {lesson_id}?"
  choices:
    - {{key: alpha, label: "Alpha choice", why: "Alpha why for L{lesson_id}."}}
    - {{key: beta, label: "Beta choice", why: "Beta why for L{lesson_id}."}}
    - {{key: gamma, label: "Gamma choice", why: "Gamma why for L{lesson_id}."}}
  answer: beta
"""
    content = f"""id: {lesson_id}
slug: lesson-{lesson_id}
title: "{title}"
type: intro
concept: "Concept for lesson {lesson_id}."
requires_tx: false
hardware: []
stages:
  frame: "Frame text for lesson {lesson_id}."
  demo: "Demo text for lesson {lesson_id}."
  try: "Try text for lesson {lesson_id}."
  predict: "Predict text for lesson {lesson_id}?"
  observe: "Observe text for lesson {lesson_id}."
  explain: "Explain text for lesson {lesson_id}."
choices:
  - {{key: one, label: "One"}}
  - {{key: two, label: "Two"}}
answer: one
verdict: "One is right."
{check_yaml}
"""
    (pack_dir / f"{lesson_id:02d}-lesson-{lesson_id}.yaml").write_text(content, encoding="utf-8")


def _make_packs(tmp_path: Path) -> Path:
    pack_dir = tmp_path / "packs"
    pack_dir.mkdir()
    _write_pack(pack_dir, 1, with_check=True)
    _write_pack(pack_dir, 2, with_check=True)
    _write_pack(pack_dir, 3, with_check=True)
    _write_pack(pack_dir, 4, with_check=False)   # completed without check
    _write_pack(pack_dir, 5, with_check=True)    # target fresh lesson
    _write_pack(pack_dir, 6, with_check=False)   # second target fresh lesson
    return pack_dir


def _dump_tables(db_path: Path, tables: tuple[str, ...]) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(str(db_path))
    try:
        out: dict[str, list[tuple]] = {}
        for table in tables:
            cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            quote_sql = ", ".join(f'quote("{c}")' for c in cols)
            order_sql = ", ".join(f'"{c}"' for c in cols)
            out[table] = conn.execute(
                f'SELECT {quote_sql} FROM "{table}" ORDER BY {order_sql}, {quote_sql}'
            ).fetchall()
        return out
    finally:
        conn.close()


ALL_NON_RETRIEVAL_TABLES = (
    "progress", "responses", "stages", "places", "stamps", "stamp_events", "sort_answers",
)


# --- Scheduler unit tests -----------------------------------------------------------------


def test_box_intervals_have_no_zero_wait_and_match_schedule():
    assert retrieval.BOX_INTERVALS == (1 * DAY, 3 * DAY, 7 * DAY, 14 * DAY, 30 * DAY)
    assert all(interval > 0 for interval in retrieval.BOX_INTERVALS)
    assert retrieval.MAX_CARDS == 2


def test_next_box_promotes_caps_at_5_and_drops_miss_to_1():
    assert retrieval.next_box(1, True) == 2
    assert retrieval.next_box(2, True) == 3
    assert retrieval.next_box(3, True) == 4
    assert retrieval.next_box(4, True) == 5
    assert retrieval.next_box(5, True) == 5
    for box in range(1, 6):
        assert retrieval.next_box(box, False) == 1


def test_due_ordering_exclusion_and_max_cards_cap():
    now = 100 * DAY
    rows = {
        1: {"box": 2, "last_seen": now - 10 * DAY},   # box 2, due
        2: {"box": 1, "last_seen": now - 2 * DAY},    # box 1, older
        3: {"box": 1, "last_seen": now - 2 * DAY},    # box 1, same age, higher id
        4: {"box": 1, "last_seen": now - 1 * DAY},    # box 1, newer
        5: {"box": 1, "last_seen": now - 5 * DAY},    # excluded
    }
    ordered = retrieval.due(rows, now=now, exclude=5)
    assert ordered == [2, 3, 4, 1]
    assert 5 not in ordered
    assert ordered[:retrieval.MAX_CARDS] == [2, 3]


def test_enrolled_card_waits_full_day_and_missed_card_waits_full_day():
    t0 = 1_700_000_000.0
    rows = {1: {"box": 1, "last_seen": t0}}
    assert retrieval.due(rows, now=t0, exclude=5) == []
    assert retrieval.due(rows, now=t0 + 23 * 3600, exclude=5) == []
    assert retrieval.due(rows, now=t0 + DAY, exclude=5) == [1]

    # Missed answer drops to box 1 at t1 -> not due at t1, due at t1 + 1 day.
    t1 = t0 + DAY + 60
    new_box = retrieval.next_box(3, False)
    assert new_box == 1
    after_miss = {1: {"box": new_box, "last_seen": t1}}
    assert retrieval.due(after_miss, now=t1, exclude=5) == []
    assert retrieval.due(after_miss, now=t1 + DAY - 1, exclude=5) == []
    assert retrieval.due(after_miss, now=t1 + DAY, exclude=5) == [1]


def test_clock_behind_last_seen_makes_nothing_due():
    t0 = 1_700_000_000.0
    rows = {
        1: {"box": 1, "last_seen": t0},
        2: {"box": 3, "last_seen": t0 - 100},
        3: {"box": 5, "last_seen": t0},
    }
    # Step back past every box's full interval so an abs(now - last_seen) regression is caught.
    assert retrieval.due(rows, now=t0 - 10_000, exclude=5) == []
    assert retrieval.due(rows, now=t0 - 2 * DAY, exclude=5) == []
    assert retrieval.due(rows, now=t0 - 31 * DAY, exclude=5) == []
    for box, wait in enumerate(retrieval.BOX_INTERVALS, start=1):
        assert retrieval.is_due(box, t0, now=t0 - wait - DAY) is False


def test_clock_stepped_back_past_wait_shows_no_card_and_post_gets_409(tmp_path):
    pack_dir = _make_packs(tmp_path)
    data_dir = tmp_path / "data"
    _seed_completed(data_dir, (1,))
    t0 = 1_700_000_000.0
    now = [t0]

    with _recall_client(data_dir, pack_dir, lambda: now[0]) as client:
        assert client.get("/lesson/5").status_code == 200  # enrol L1 at t0
        snap = _dump_tables(data_dir / "progress.db", (*ALL_NON_RETRIEVAL_TABLES, "retrieval"))

        # Step the clock back 2 full days (exceeding box 1's 1-day wait).
        now[0] = t0 - 2 * DAY
        resp = client.get("/lesson/5")
        assert resp.status_code == 200
        assert 'data-stage="recall"' not in resp.text
        assert 'data-stage="frame"' in resp.text

        post = client.post(
            "/api/recall?lesson_id=5&source=1&card=1",
            data={"choice": "beta"},
            follow_redirects=False,
        )
        assert post.status_code == 409
        assert _dump_tables(
            data_dir / "progress.db", (*ALL_NON_RETRIEVAL_TABLES, "retrieval")
        ) == snap


def test_managed_mode_recall_syncs_event_and_answers_500_on_sync_failure(tmp_path, monkeypatch):
    pack_dir = _make_packs(tmp_path)
    gen_dir = tmp_path / "managed_gen"
    gen_dir.mkdir()
    scratch = tmp_path / "scratch"
    migrate_store_copy(
        tmp_path / "missing_progress.db", gen_dir / "progress.db",
        store="progress", allow_missing_source=True, scratch_dir=scratch,
    )
    migrate_store_copy(
        tmp_path / "missing_exam.db", gen_dir / "exam.db",
        store="exam", allow_missing_source=True, scratch_dir=scratch,
    )

    now = [1_700_000_000.0]
    app = create_app(
        pack_dir=pack_dir,
        data_dir=gen_dir,
        probe=lambda: RadioProbe(present=False, reason="test"),
        clock=lambda: now[0],
        managed=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # Complete L1 through real routes and open L5 to enrol L1 in managed mode.
        assert client.post(
            "/api/predict?lesson_id=1", data={"prediction": "one"}, follow_redirects=False
        ).status_code == 303
        assert client.post("/api/complete?lesson_id=1").status_code == 200
        assert client.get("/lesson/5").status_code == 200

        # One day later, answering recall must fsync events.jsonl before replying 303.
        now[0] += DAY
        fsync_calls: list[str] = []
        real_fsync = events_mod.fsync_handle

        def spy_fsync(handle):
            fsync_calls.append("file")
            return real_fsync(handle)

        monkeypatch.setattr(events_mod, "fsync_handle", spy_fsync)
        ok = client.post(
            "/api/recall?lesson_id=5&source=1&card=1",
            data={"choice": "beta"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert fsync_calls == ["file"]

        # Three days later (box 2 due), a sync OSError must answer 500 JSON like /api/check.
        now[0] += 3 * DAY

        def fail_fsync(_handle):
            raise OSError("disk full")

        monkeypatch.setattr(events_mod, "fsync_handle", fail_fsync)
        failed = client.post(
            "/api/recall?lesson_id=5&source=1&card=1",
            data={"choice": "beta"},
            follow_redirects=False,
        )
        assert failed.status_code == 500
        assert failed.json() == {
            "recorded": False,
            "reason": "event log sync failed: disk full",
        }

    # In legacy mode, an event-log OSError propagates without rewriting as "event log sync failed".
    legacy_dir = tmp_path / "legacy_err"
    _seed_completed(legacy_dir, (1,))
    legacy_now = [1_700_000_000.0]
    legacy_app = create_app(
        pack_dir=pack_dir,
        data_dir=legacy_dir,
        probe=lambda: RadioProbe(present=False, reason="test"),
        clock=lambda: legacy_now[0],
        managed=False,
    )
    with TestClient(legacy_app, raise_server_exceptions=False) as client:
        assert client.get("/lesson/5").status_code == 200
        legacy_now[0] += DAY

        def boom(*_args, **_kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr("sigint101.app.log_event", boom)
        res = client.post(
            "/api/recall?lesson_id=5&source=1&card=1",
            data={"choice": "beta"},
            follow_redirects=False,
        )
        assert res.status_code == 500
        assert "event log sync failed" not in res.text


def test_retrieval_module_is_pure_no_sqlite_or_exam_imports():
    tree = ast.parse((REPO / "sigint101" / "retrieval.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            for alias in node.names:
                imported.add(alias.name)
    assert "sqlite3" not in imported
    assert "sigint101.exam" not in imported
    assert "exam" not in imported
    assert "ExamStore" not in imported


# --- Route & integration tests ------------------------------------------------------------


def _seed_completed(data_dir: Path, lesson_ids: tuple[int, ...]) -> None:
    prog = Progress(data_dir / "progress.db")
    for lid in lesson_ids:
        prog.mark_completed(lid)
    prog.close()


def test_only_completed_lessons_with_check_feed_cards_and_never_self(tmp_path):
    pack_dir = _make_packs(tmp_path)
    data_dir = tmp_path / "data"
    # L1 completed (has check), L2 incomplete (has check), L4 completed (no check),
    # L5 completed (has check) -> opening L5 must NOT serve L5, only L1!
    _seed_completed(data_dir, (1, 4, 5))
    now = [1_700_000_000.0]

    with _recall_client(data_dir, pack_dir, lambda: now[0]) as client:
        # First open enrols L1 and L5 at t0 -> nothing due yet, opens L5 on frame.
        first = client.get("/lesson/5")
        assert first.status_code == 200
        assert 'data-stage="recall"' not in first.text
        assert 'data-stage="frame"' in first.text

        # Advance 1 day: L1 and L5 are both past 1 day, but opening L5 fresh excludes L5.
        # L2 is incomplete and L4 has no check, so ONLY L1 is served (card 1 of 1).
        now[0] += DAY
        due_resp = client.get("/lesson/5")
        assert due_resp.status_code == 200
        assert 'data-stage="recall"' in due_resp.text
        assert "RECALL &middot; CARD 1 OF 1 &middot; FROM LESSON 1" in due_resp.text
        assert "Check prompt for lesson 1?" in due_resp.text
        assert "Check prompt for lesson 5?" not in due_resp.text
        assert "Check prompt for lesson 2?" not in due_resp.text


def test_saved_stage_or_prediction_never_shows_recall_card(tmp_path):
    pack_dir = _make_packs(tmp_path)
    data_dir = tmp_path / "data"
    _seed_completed(data_dir, (1,))
    now = [1_700_000_000.0]

    with _recall_client(data_dir, pack_dir, lambda: now[0]) as client:
        # Enrol L1.
        client.get("/lesson/6")
        now[0] += DAY

        # Give L5 a saved stage -> never shows recall card.
        client.app.state.progress.record_stage(5, "demo", 1)
        resp_stage = client.get("/lesson/5")
        assert resp_stage.status_code == 200
        assert 'data-stage="recall"' not in resp_stage.text
        assert 'data-stage="demo"' in resp_stage.text

        # Give L6 a prediction -> POST /api/predict 303 and GET /lesson/6 never meet recall.
        pred = client.post("/api/predict?lesson_id=6", data={"prediction": "one"},
                           follow_redirects=False)
        assert pred.status_code == 303
        assert pred.headers["location"] == "/lesson/6"
        resp_pred = client.get("/lesson/6")
        assert 'data-stage="recall"' not in resp_pred.text
        assert 'data-stage="observe"' in resp_pred.text


def test_stage_order_rail_pips_and_furthest_unchanged_with_due_card(tmp_path):
    pack_dir = _make_packs(tmp_path)
    data_dir = tmp_path / "data"
    _seed_completed(data_dir, (1,))
    now = [1_700_000_000.0]

    with _recall_client(data_dir, pack_dir, lambda: now[0]) as client:
        without = client.get("/lesson/5").text
        order_before = client.app.state.lessons[5].stage_order
        pips_before = len(re.findall(r'class="rail-pip\b', without))
        assert order_before == ("frame", "demo", "try", "predict", "observe", "explain", "check")
        assert pips_before == 7

        now[0] += DAY
        # Recall card has no rail at all.
        card_html = client.get("/lesson/5").text
        assert 'data-stage="recall"' in card_html
        assert 'class="rail-pip' not in card_html

        # Skipping into the lesson keeps identical stage_order, rail pips, and stages.furthest.
        skipped = client.get("/lesson/5?recall=skip").text
        assert client.app.state.lessons[5].stage_order == order_before
        assert len(re.findall(r'class="rail-pip\b', skipped)) == pips_before
        assert client.app.state.progress.get_stage(5) is None


def test_two_card_queue_never_serves_third_card_and_feedback_shows_right_and_missed(tmp_path):
    pack_dir = _make_packs(tmp_path)
    data_dir = tmp_path / "data"
    _seed_completed(data_dir, (1, 2, 3))
    now = [1_700_000_000.0]

    with _recall_client(data_dir, pack_dir, lambda: now[0]) as client:
        client.get("/lesson/5")   # enrol L1, L2, L3 at t0
        now[0] += DAY

        # Card 1 of 2 (L1): answer wrong (alpha instead of beta).
        c1 = client.get("/lesson/5").text
        assert "RECALL &middot; CARD 1 OF 2 &middot; FROM LESSON 1" in c1
        assert "/lesson/5?recall=skip" in c1
        post1 = client.post(
            "/api/recall?lesson_id=5&source=1&card=1",
            data={"choice": "alpha"},
            follow_redirects=False,
        )
        assert post1.status_code == 303
        assert post1.headers["location"] == "/lesson/5?recall=answered&source=1&card=1"

        fb1 = client.get(post1.headers["location"]).text
        assert "Missed" in fb1
        assert "verdict--miss" in fb1
        assert "Alpha why for L1." in fb1
        assert "Beta why for L1." in fb1
        assert "predict-chip--answer" in fb1
        assert "/lesson/5?recall=skip" in fb1
        assert 'href="/lesson/5?recall=card&amp;card=2"' in fb1
        assert "Next card" in fb1

        # Reloading card 1 or jumping to card 2 must NOT re-serve missed L1 (waits 1 day).
        c2 = client.get("/lesson/5?recall=card&card=2").text
        assert "RECALL &middot; CARD 2 OF 2 &middot; FROM LESSON 2" in c2
        assert "Check prompt for lesson 2?" in c2
        assert "Check prompt for lesson 1?" not in c2

        # Card 2 of 2 (L2): answer right (beta).
        post2 = client.post(
            "/api/recall?lesson_id=5&source=2&card=2",
            data={"choice": "beta"},
            follow_redirects=False,
        )
        assert post2.status_code == 303
        assert post2.headers["location"] == "/lesson/5?recall=answered&source=2&card=2"

        fb2 = client.get(post2.headers["location"]).text
        assert "Right" in fb2
        assert "verdict--hit" in fb2
        assert fb2.count("Beta why for L2.") == 1
        assert "Alpha why for L2." not in fb2
        assert "predict-chip--answer" not in fb2
        assert "/lesson/5?recall=skip" in fb2
        # Even though L3 is still due, card 2 ends with Start lesson, never a 3rd card!
        assert 'href="/lesson/5?recall=done"' in fb2
        assert "Start lesson" in fb2
        assert "Next card" not in fb2

        # Start lesson lands on the lesson's first stage.
        started = client.get("/lesson/5?recall=done").text
        assert 'data-stage="recall"' not in started
        assert 'data-stage="frame"' in started

        # Malformed or unknown recall query params fall through cleanly to the lesson (no error).
        for bad_qs in ("?recall=bogus", "?recall=answered&source=999&card=1",
                       "?recall=answered&source=3&card=1", "?recall=answered&source=abc&card=x",
                       "?recall=card&card=3"):
            fallback = client.get(f"/lesson/5{bad_qs}")
            assert fallback.status_code == 200
            assert 'data-stage="recall"' not in fallback.text
            assert 'data-stage="frame"' in fallback.text


def test_skip_writes_nothing_and_leaves_card_due(tmp_path):
    pack_dir = _make_packs(tmp_path)
    data_dir = tmp_path / "data"
    _seed_completed(data_dir, (1,))
    now = [1_700_000_000.0]

    with _recall_client(data_dir, pack_dir, lambda: now[0]) as client:
        client.get("/lesson/5")
        now[0] += DAY
        card = client.get("/lesson/5")
        assert 'data-stage="recall"' in card.text

        db_path = data_dir / "progress.db"
        exam_path = data_dir / "exam.db"
        tables_before = _dump_tables(db_path, (*ALL_NON_RETRIEVAL_TABLES, "retrieval"))
        exam_before = exam_path.read_bytes()
        events_before = read_events(data_dir / "events.jsonl")

        skipped = client.get("/lesson/5?recall=skip")
        assert skipped.status_code == 200
        assert 'data-stage="frame"' in skipped.text
        assert 'data-stage="recall"' not in skipped.text

        assert _dump_tables(db_path, (*ALL_NON_RETRIEVAL_TABLES, "retrieval")) == tables_before
        assert exam_path.read_bytes() == exam_before
        assert read_events(data_dir / "events.jsonl") == events_before

        # Card is still due on the next fresh opening.
        again = client.get("/lesson/5")
        assert 'data-stage="recall"' in again.text


def test_answering_recall_leaves_other_tables_and_exam_db_unchanged(tmp_path):
    pack_dir = _make_packs(tmp_path)
    data_dir = tmp_path / "data"
    _seed_completed(data_dir, (1,))
    now = [1_700_000_000.0]

    with _recall_client(data_dir, pack_dir, lambda: now[0]) as client:
        client.get("/lesson/5")
        now[0] += DAY
        client.get("/lesson/5")

        db_path = data_dir / "progress.db"
        exam_path = data_dir / "exam.db"
        before_tables = _dump_tables(db_path, ALL_NON_RETRIEVAL_TABLES)
        before_exam = exam_path.read_bytes()

        resp = client.post(
            "/api/recall?lesson_id=5&source=1&card=1",
            data={"choice": "beta"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        assert _dump_tables(db_path, ALL_NON_RETRIEVAL_TABLES) == before_tables
        assert exam_path.read_bytes() == before_exam

        events = read_events(data_dir / "events.jsonl")
        recall_events = [e for e in events if e["kind"] == "recall"]
        check_events = [e for e in events if e["kind"] == "check"]
        assert len(recall_events) == 1
        assert recall_events[0]["payload"] == {"lesson": 5, "source": 1, "correct": True, "box": 2}
        assert check_events == []


def test_m5_m12_completed_before_check_keeps_own_check_open_and_answered_check_intact(tmp_path):
    pack_dir = _make_packs(tmp_path)
    data_dir = tmp_path / "data"
    # L1: completed before it had a check (no responses check row, only prediction + completed).
    # L2: completed AND its own check was already answered ("alpha").
    prog = Progress(data_dir / "progress.db")
    prog.record_prediction(1, "one", correct=True)
    prog.mark_completed(1)
    prog.record_prediction(2, "one", correct=True)
    prog.record_check(2, "alpha", correct=False)
    prog.mark_completed(2)
    prog.close()

    now = [1_700_000_000.0]
    with _recall_client(data_dir, pack_dir, lambda: now[0]) as client:
        client.get("/lesson/5")
        now[0] += DAY

        # Answer recall for L1 (right -> beta) and L2 (right -> beta).
        r1 = client.post("/api/recall?lesson_id=5&source=1&card=1",
                         data={"choice": "beta"}, follow_redirects=False)
        assert r1.status_code == 303
        r2 = client.post("/api/recall?lesson_id=5&source=2&card=2",
                         data={"choice": "beta"}, follow_redirects=False)
        assert r2.status_code == 303

        # L1's own check is STILL None and POST /api/check succeeds (303, not 409).
        assert client.app.state.progress.get_check(1) is None
        own_check = client.post("/api/check?lesson_id=1", data={"choice": "alpha"},
                                follow_redirects=False)
        assert own_check.status_code == 303
        assert client.app.state.progress.get_check(1) == {"choice": "alpha", "correct": False}

        # L2's previously answered check is unchanged.
        assert client.app.state.progress.get_check(2) == {"choice": "alpha", "correct": False}


def test_recall_post_refusals_write_nothing(tmp_path):
    pack_dir = _make_packs(tmp_path)
    data_dir = tmp_path / "data"
    _seed_completed(data_dir, (1, 4))
    now = [1_700_000_000.0]

    with _recall_client(data_dir, pack_dir, lambda: now[0]) as client:
        client.get("/lesson/5")   # enrol L1 at t0 (not yet due)

        db_path = data_dir / "progress.db"
        snap = _dump_tables(db_path, (*ALL_NON_RETRIEVAL_TABLES, "retrieval"))

        # Unknown lesson_id -> 404.
        assert client.post("/api/recall?lesson_id=999&source=1&card=1",
                           data={"choice": "beta"}).status_code == 404
        # Bad source: not loaded, no check (L4), incomplete (L2), self (L5) -> 400.
        for bad_src in (999, 4, 2, 5):
            assert client.post(f"/api/recall?lesson_id=5&source={bad_src}&card=1",
                               data={"choice": "beta"}).status_code == 400
        # Bad card -> 400.
        for bad_card in (0, 3, -1):
            assert client.post(f"/api/recall?lesson_id=5&source=1&card={bad_card}",
                               data={"choice": "beta"}).status_code == 400
        # Bad choice -> 400.
        assert client.post("/api/recall?lesson_id=5&source=1&card=1",
                           data={"choice": "nope"}).status_code == 400
        # Not yet due -> 409.
        assert client.post("/api/recall?lesson_id=5&source=1&card=1",
                           data={"choice": "beta"}).status_code == 409

        # None of those refusals wrote anything or logged a recall event.
        assert _dump_tables(db_path, (*ALL_NON_RETRIEVAL_TABLES, "retrieval")) == snap
        assert [e for e in read_events(data_dir / "events.jsonl") if e["kind"] == "recall"] == []

        # Advance 1 day, answer once (303), then resubmit -> 409 (cannot climb twice).
        now[0] += DAY
        assert client.post("/api/recall?lesson_id=5&source=1&card=1",
                           data={"choice": "beta"}, follow_redirects=False).status_code == 303
        after_first = _dump_tables(db_path, ("retrieval",))
        assert client.post("/api/recall?lesson_id=5&source=1&card=1",
                           data={"choice": "beta"}, follow_redirects=False).status_code == 409
        assert _dump_tables(db_path, ("retrieval",)) == after_first
        # Verify unescape check on recall page uses the lesson's own title and Back.
        fb = client.get("/lesson/5?recall=answered&source=1&card=1").text
        assert "Lesson 5" in unescape(fb)
        assert 'href="/unit/2"' in fb
