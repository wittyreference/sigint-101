# ABOUTME: Unit tests for the SQLite progress store.
# ABOUTME: Opening and completing a lesson persists across separate connections.

from pathlib import Path

import pytest

from sigint101.progress import Progress


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


def test_a_second_prediction_replaces_the_first(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    prog.record_prediction(1, "first guess")
    prog.record_prediction(1, "second guess")
    assert prog.get_prediction(1) == "second guess"
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
    # reached: the rail keeps every visited stage tappable.
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
