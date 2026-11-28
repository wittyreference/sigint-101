# ABOUTME: Unit tests for the SQLite progress store.
# ABOUTME: Opening and completing a lesson persists across separate connections.

from pathlib import Path

import pytest

from sigint101.progress import PredictionAlreadyCommitted, Progress


def test_open_and_complete(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    assert prog.get(1) is None
    prog.mark_opened(1)
    row = prog.get(1)
    assert row["opened"] == 1
    assert row["completed"] == 0
    prog.mark_completed(1)
    row = prog.get(1)
    assert row["completed"] == 1
    assert row["opened"] == 1
    prog.close()


def test_persists_across_connections(tmp_path):
    db = tmp_path / "progress.db"
    first = Progress(db)
    first.mark_opened(3)
    first.close()
    second = Progress(db)
    assert second.get(3)["opened"] == 1
    second.close()


def test_prediction_roundtrip(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    assert prog.get_prediction(1) is None
    prog.record_prediction(1, "  a fat block about 200 kHz wide  ")
    assert prog.get_prediction(1) == "a fat block about 200 kHz wide"
    prog.close()


def test_recording_a_prediction_opens_the_lesson(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    prog.record_prediction(2, "a thin line")
    assert prog.get(2)["opened"] == 1
    prog.close()


def test_get_carries_the_prediction(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    prog.mark_opened(1)
    assert prog.get(1)["prediction"] is None
    prog.record_prediction(1, "scattered dots")
    assert prog.get(1)["prediction"] == "scattered dots"
    prog.close()


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_a_blank_prediction_is_refused(tmp_path, blank):
    # Fails closed like the other gates: a blank answer is not a prediction, and
    # accepting one would unlock the answer without the learner having committed.
    prog = Progress(tmp_path / "progress.db")
    with pytest.raises(ValueError):
        prog.record_prediction(1, blank)
    assert prog.get_prediction(1) is None
    prog.close()


def test_a_second_prediction_is_refused_and_the_first_stands(tmp_path):
    # A committed prediction is the learner's word before the reveal; once the explain has shown
    # the answer, letting them overwrite it would turn the score into a copy of the reveal.
    prog = Progress(tmp_path / "progress.db")
    prog.record_prediction(1, "first guess", correct=False)
    with pytest.raises(PredictionAlreadyCommitted):
        prog.record_prediction(1, "second guess", correct=True)
    assert prog.get_prediction(1) == "first guess"
    assert prog.get_correct(1) is False
    prog.close()


def test_a_prediction_stores_whether_it_was_right(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    prog.record_prediction(1, "same", correct=True)
    prog.record_prediction(2, "wider", correct=False)
    assert prog.get_correct(1) is True
    assert prog.get_correct(2) is False
    prog.close()


def test_an_unscored_prediction_stores_no_verdict(tmp_path):
    # A pack with no answer key cannot be scored; the store says "unknown", never a guessed False.
    prog = Progress(tmp_path / "progress.db")
    prog.record_prediction(1, "a free answer")
    assert prog.get_correct(1) is None
    assert prog.get_correct(9) is None
    prog.close()


def test_correctness_persists_across_connections(tmp_path):
    db = tmp_path / "progress.db"
    first = Progress(db)
    first.record_prediction(4, "bigger", correct=True)
    first.close()
    second = Progress(db)
    assert second.get_correct(4) is True
    second.close()


def test_an_existing_responses_table_gains_the_correct_column(tmp_path):
    # A box that ran a build before scoring has a responses table with no `correct` column. Opening
    # it must add the column and keep the predictions already there, not fail or drop them.
    db = tmp_path / "progress.db"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE responses (lesson_id INTEGER NOT NULL, kind TEXT NOT NULL, "
        "body TEXT NOT NULL, updated REAL, PRIMARY KEY (lesson_id, kind));"
        "INSERT INTO responses(lesson_id, kind, body) VALUES(1, 'prediction', 'old guess');"
    )
    conn.commit()
    conn.close()

    prog = Progress(db)
    assert prog.get_prediction(1) == "old guess"
    assert prog.get_correct(1) is None
    prog.record_prediction(2, "same", correct=True)
    assert prog.get_correct(2) is True
    prog.close()


def test_prediction_persists_across_connections(tmp_path):
    db = tmp_path / "progress.db"
    first = Progress(db)
    first.record_prediction(4, "a bright band")
    first.close()
    second = Progress(db)
    assert second.get_prediction(4) == "a bright band"
    second.close()


def test_an_existing_db_without_the_responses_table_is_upgraded(tmp_path):
    # A learner who ran an earlier build already has data/progress.db. Opening it must
    # add the new table rather than fail or lose their progress.
    db = tmp_path / "progress.db"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE progress (lesson_id INTEGER PRIMARY KEY, opened INTEGER NOT NULL "
        "DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0, updated REAL);"
        "INSERT INTO progress(lesson_id, opened) VALUES(1, 1);"
    )
    conn.commit()
    conn.close()

    prog = Progress(db)
    assert prog.get(1)["opened"] == 1
    prog.record_prediction(1, "still works")
    assert prog.get_prediction(1) == "still works"
    prog.close()


def test_the_store_defaults_under_data(tmp_path, monkeypatch):
    # The default path lives under data/, which .gitignore keeps out of the repo so a
    # learner's progress is never committed. Exercise the default from inside a temp cwd
    # so the test never writes the real shared database.
    from sigint101.progress import DEFAULT_DB_PATH

    assert Path(DEFAULT_DB_PATH).parent.name == "data"
    monkeypatch.chdir(tmp_path)
    prog = Progress()
    assert Path(DEFAULT_DB_PATH).exists()
    prog.close()


# --- where the learner is inside a lesson (resume after a reload or a power cut) ---------------


def test_a_lesson_with_no_stage_recorded_has_none(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    assert prog.get_stage(1) is None
    prog.close()


def test_recording_a_stage_remembers_it_and_how_far_the_learner_got(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    prog.record_stage(1, "demo", 1)
    assert prog.get_stage(1) == {"stage": "demo", "furthest": 1}
    prog.record_stage(1, "try", 2)
    assert prog.get_stage(1) == {"stage": "try", "furthest": 2}
    prog.close()


def test_stepping_back_keeps_the_furthest_stage_reached(tmp_path):
    # Re-reading the setup after reaching the explanation must not forget the explanation was
    # reached: the progress line keeps every visited stage marked.
    prog = Progress(tmp_path / "progress.db")
    prog.record_stage(1, "explain", 5)
    prog.record_stage(1, "frame", 0)
    assert prog.get_stage(1) == {"stage": "frame", "furthest": 5}
    prog.close()


def test_stages_are_kept_per_lesson(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    prog.record_stage(1, "try", 2)
    prog.record_stage(2, "demo", 1)
    assert prog.get_stage(1)["stage"] == "try"
    assert prog.get_stage(2)["stage"] == "demo"
    prog.close()


def test_the_stage_survives_a_reopen(tmp_path):
    # The kiosk loses power in the field; the place in the lesson must survive it.
    db = tmp_path / "progress.db"
    first = Progress(db)
    first.record_stage(3, "predict", 3)
    first.close()
    second = Progress(db)
    assert second.get_stage(3) == {"stage": "predict", "furthest": 3}
    second.close()


@pytest.mark.parametrize("stage, position", [("", 0), ("   ", 1), ("demo", -1)])
def test_a_blank_stage_or_negative_position_is_refused(tmp_path, stage, position):
    prog = Progress(tmp_path / "progress.db")
    with pytest.raises(ValueError):
        prog.record_stage(1, stage, position)
    assert prog.get_stage(1) is None
    prog.close()


def test_an_existing_db_from_before_stages_were_kept_still_opens(tmp_path):
    # A box that ran an earlier build has progress and predictions but no stage table. Opening it
    # must keep that history and start keeping stages, with no migration step.
    db = tmp_path / "progress.db"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE progress (lesson_id INTEGER PRIMARY KEY, opened INTEGER NOT NULL "
        "DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0, updated REAL);"
        "CREATE TABLE responses (lesson_id INTEGER NOT NULL, kind TEXT NOT NULL, "
        "body TEXT NOT NULL, updated REAL, PRIMARY KEY (lesson_id, kind));"
        "INSERT INTO progress(lesson_id, opened, completed) VALUES(1, 1, 1);"
        "INSERT INTO responses(lesson_id, kind, body) VALUES(1, 'prediction', 'b');"
    )
    conn.commit()
    conn.close()

    prog = Progress(db)
    assert prog.get(1)["completed"] == 1
    assert prog.get_prediction(1) == "b"
    assert prog.get_stage(1) is None
    prog.record_stage(1, "observe", 4)
    assert prog.get_stage(1) == {"stage": "observe", "furthest": 4}
    prog.close()


# --- places outside a lesson: where the rack was left -----------------------------------------


def test_a_place_is_none_until_one_is_recorded(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    assert prog.get_place("rack") is None
    prog.close()


def test_the_latest_place_wins_and_survives_a_reopen(tmp_path):
    db = tmp_path / "progress.db"
    first = Progress(db)
    first.record_place("rack", "aprs")
    first.record_place("rack", "ism")
    first.close()
    second = Progress(db)
    assert second.get_place("rack") == "ism"
    second.close()


def test_a_blank_place_is_refused(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    with pytest.raises(ValueError):
        prog.record_place("rack", "  ")
    prog.close()


# --- retrieval cards: additive table and locked writes ----------------------------------------


def _dump_tables(db_path: Path, tables: tuple[str, ...]) -> dict[str, list[tuple]]:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in tables
        }
    finally:
        conn.close()


def test_preexisting_progress_db_gains_retrieval_with_every_old_row_unchanged(tmp_path):
    import sqlite3

    db = tmp_path / "progress.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE progress (lesson_id INTEGER PRIMARY KEY, opened INTEGER NOT NULL DEFAULT 0,"
        " completed INTEGER NOT NULL DEFAULT 0, updated REAL);"
        "CREATE TABLE responses (lesson_id INTEGER NOT NULL, kind TEXT NOT NULL,"
        " body TEXT NOT NULL, updated REAL, correct INTEGER, PRIMARY KEY (lesson_id, kind));"
        "CREATE TABLE stages (lesson_id INTEGER PRIMARY KEY, stage TEXT NOT NULL,"
        " furthest INTEGER NOT NULL DEFAULT 0, updated REAL);"
        "CREATE TABLE places (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL);"
        "CREATE TABLE stamps (instrument TEXT PRIMARY KEY, first_ts REAL NOT NULL,"
        " count INTEGER NOT NULL, provenance TEXT NOT NULL, digest TEXT NOT NULL);"
        "CREATE TABLE stamp_events (digest BLOB PRIMARY KEY) WITHOUT ROWID;"
        "CREATE TABLE sort_answers (lesson_id INTEGER NOT NULL, card_key TEXT NOT NULL,"
        " choice TEXT NOT NULL, correct INTEGER NOT NULL, updated REAL,"
        " PRIMARY KEY (lesson_id, card_key));"
        "INSERT INTO progress VALUES(1, 1, 1, 100.0);"
        "INSERT INTO responses VALUES(1, 'prediction', 'a', 100.0, 1);"
        "INSERT INTO responses VALUES(1, 'check', 'b', 101.0, 1);"
        "INSERT INTO stages VALUES(1, 'check', 6, 101.0);"
        "INSERT INTO places VALUES('rack', 'ism', 102.0);"
        "INSERT INTO stamps VALUES('ism', 103.0, 1, '{\"tool\":\"rtl_433\"}', 'aa');"
        "INSERT INTO stamp_events VALUES(X'aa');"
        "INSERT INTO sort_answers VALUES(5, 'fm-rx', 'legal', 1, 104.0);"
    )
    conn.commit()
    conn.close()

    old_tables = (
        "progress", "responses", "stages", "places", "stamps", "stamp_events", "sort_answers",
    )
    before = _dump_tables(db, old_tables)

    prog = Progress(db)
    assert prog.get_retrieval([1]) == {}
    prog.close()

    assert _dump_tables(db, old_tables) == before
    conn = sqlite3.connect(str(db))
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(retrieval)")]
    finally:
        conn.close()
    assert cols == ["lesson_id", "box", "last_seen", "seen", "correct", "last_choice"]


def test_enroll_recall_never_overwrites_an_existing_row(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    t0 = 1_700_000_000.0
    prog.enroll_recall([1, 2], now=t0)
    assert prog.get_retrieval([1, 2]) == {
        1: {"lesson_id": 1, "box": 1, "last_seen": t0, "seen": 0, "correct": 0,
            "last_choice": None},
        2: {"lesson_id": 2, "box": 1, "last_seen": t0, "seen": 0, "correct": 0,
            "last_choice": None},
    }
    prog.record_recall(1, "beta", correct=True, now=t0 + 86_400)
    prog.enroll_recall([1, 2, 3], now=t0 + 2 * 86_400)
    rows = prog.get_retrieval([1, 2, 3])
    assert rows[1]["box"] == 2 and rows[1]["seen"] == 1 and rows[1]["last_choice"] == "beta"
    assert rows[2]["last_seen"] == t0
    assert rows[3]["last_seen"] == t0 + 2 * 86_400
    prog.close()


def test_record_recall_writes_only_retrieval_and_not_due_writes_nothing(tmp_path):
    from sigint101.progress import RecallNotDue

    db = tmp_path / "progress.db"
    prog = Progress(db)
    prog.mark_completed(1)
    prog.record_prediction(1, "a", correct=True)
    prog.record_stage(1, "explain", 5)
    prog.record_place("rack", "ism")
    prog.record_sort_answer(5, "fm-rx", "legal", correct=True)
    prog.record_stamps(({
        "instrument": "ism", "first_ts": 100.0, "provenance": {"tool": "rtl_433"},
        "digest": "00" * 32,
    },))

    other_tables = (
        "progress", "responses", "stages", "places", "stamps", "stamp_events", "sort_answers",
    )
    t0 = 1_700_000_000.0
    prog.enroll_recall([1], now=t0)
    before_others = _dump_tables(db, other_tables)
    before_ret = _dump_tables(db, ("retrieval",))

    # Missing row or not yet due -> RecallNotDue and zero writes.
    with pytest.raises(RecallNotDue):
        prog.record_recall(99, "beta", correct=True, now=t0 + 86_400)
    with pytest.raises(RecallNotDue):
        prog.record_recall(1, "beta", correct=True, now=t0 + 3600)
    assert _dump_tables(db, ("retrieval",)) == before_ret
    assert _dump_tables(db, other_tables) == before_others

    # Due at +1 day -> updates ONLY retrieval.
    new_box = prog.record_recall(1, "beta", correct=True, now=t0 + 86_400)
    assert new_box == 2
    assert _dump_tables(db, other_tables) == before_others
    row = prog.get_retrieval([1])[1]
    assert (row["box"], row["seen"], row["correct"], row["last_choice"]) == (2, 1, 1, "beta")
    prog.close()
