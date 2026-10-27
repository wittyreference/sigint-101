# ABOUTME: Tests for the Technician exam engine — pool/errata guard, Leitner scheduler,
# ABOUTME: practice-exam size/distribution, and 26/35 scoring.

import dataclasses
import json
from pathlib import Path

import pytest

from sigint101 import exam

# The real Technician exam draws one question from each of the pool's 35 groups, which
# yields this per-subelement spread. Hard-coded here so a change to the engine's blueprint
# is caught against the published NCVEC exam design, not against the engine's own opinion.
EXPECTED_BLUEPRINT = {
    "T1": 6, "T2": 3, "T3": 3, "T4": 2, "T5": 4,
    "T6": 4, "T7": 4, "T8": 4, "T9": 2, "T0": 3,
}


# --------------------------------------------------------------------------- pool + errata

def test_pool_loads_with_the_official_count():
    pool = exam.load_pool()
    assert pool.count == exam.EXPECTED_COUNT == 409


def test_shipped_pool_verifies():
    # The correctness guard passes on the committed pool: 409 questions and the four
    # errata questions carrying their corrected text and answers.
    assert exam.load_pool().verify() is True


def test_errata_ids_present_with_corrected_answers():
    pool = exam.load_pool()
    for qid, (answer, text) in exam.ERRATA.items():
        q = pool.get(qid)
        assert q is not None, f"{qid} missing from pool"
        assert q.answer == answer, f"{qid} wrong correct answer"
        assert q.question == text, f"{qid} does not carry the corrected question text"


def test_correct_option_text_resolves():
    pool = exam.load_pool()
    q = pool.get("T1C01")
    assert q.correct_option == q.options[q.answer]


def test_figure_questions_are_tagged():
    pool = exam.load_pool()
    figs = {q.figure for q in pool.questions if q.figure}
    assert figs == {"T-1", "T-2", "T-3"}


def test_loader_raises_on_missing_file(tmp_path):
    with pytest.raises(exam.PoolError):
        exam.load_pool(tmp_path / "nope.json")


def test_loader_raises_on_empty_pool(tmp_path):
    # No fabrication: an empty pool file is reported honestly, never backfilled.
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"questions": []}), encoding="utf-8")
    with pytest.raises(exam.PoolError):
        exam.load_pool(p)


def test_verify_rejects_a_short_pool():
    pool = exam.load_pool()
    short = exam.Pool(pool.questions[:10], pool.meta)
    with pytest.raises(exam.PoolError):
        short.verify()


def test_verify_rejects_a_missing_errata_question():
    # Count stays at 409 but an errata id is absent, so the guard must fail on the errata
    # check rather than the count check — the third way a pool can be quietly wrong.
    pool = exam.load_pool()
    questions = list(pool.questions)
    for i, q in enumerate(questions):
        if q.id == "T5A05":
            questions[i] = dataclasses.replace(q, id="T5A99")
            break
    with pytest.raises(exam.PoolError):
        exam.Pool(questions, pool.meta).verify()


def test_verify_rejects_a_wrong_errata_answer():
    # A pool of the right size but with a corrupted errata answer must fail the guard —
    # this is the case that would silently teach a wrong answer.
    pool = exam.load_pool()
    questions = list(pool.questions)
    for i, q in enumerate(questions):
        if q.id == "T1C01":
            wrong = "A" if q.answer != "A" else "B"
            questions[i] = dataclasses.replace(q, answer=wrong)
            break
    with pytest.raises(exam.PoolError):
        exam.Pool(questions, pool.meta).verify()


def test_verify_rejects_pre_errata_question_text():
    # The pre-errata wording with the right count and answer letters is the real trap the
    # web is full of; the guard must reject it on the question text.
    pool = exam.load_pool()
    questions = list(pool.questions)
    for i, q in enumerate(questions):
        if q.id == "T0A10":
            stale = dataclasses.replace(
                q, question="What hazard occurs when rapidly charging or discharging an "
                "unprotected battery?"
            )
            questions[i] = stale
            break
    with pytest.raises(exam.PoolError):
        exam.Pool(questions, pool.meta).verify()


def test_pool_blueprint_matches_the_exam_design():
    assert exam.load_pool().blueprint() == EXPECTED_BLUEPRINT


def test_pool_has_35_groups():
    assert len(exam.load_pool().groups()) == 35


# ------------------------------------------------------------------- Leitner scheduler

def test_new_question_starts_in_box_one(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    assert store.box_of("T1A01") == 1
    store.close()


def test_correct_answer_promotes(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    assert store.record("T1A01", True) == 2
    assert store.record("T1A01", True) == 3
    assert store.box_of("T1A01") == 3
    store.close()


def test_wrong_answer_demotes_to_box_one(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    store.record("T1A01", True)
    store.record("T1A01", True)
    assert store.box_of("T1A01") == 3
    assert store.record("T1A01", False) == 1
    assert store.box_of("T1A01") == 1
    store.close()


def test_promotion_caps_at_max_box(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    for _ in range(20):
        store.record("T1A01", True)
    assert store.box_of("T1A01") == exam.LEITNER_BOXES
    store.close()


def test_state_tracks_counts(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    store.record("T1A01", True)
    store.record("T1A01", False)
    store.record("T1A01", True)
    st = store.state("T1A01")
    assert st["seen"] == 3
    assert st["correct"] == 2
    store.close()


def test_state_persists_across_connections(tmp_path):
    db = tmp_path / "exam.db"
    first = exam.ExamStore(db)
    first.record("T5A05", True)
    first.record("T5A05", True)
    first.close()
    second = exam.ExamStore(db)
    assert second.box_of("T5A05") == 3
    second.close()


def test_due_includes_unseen_questions(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    due = store.due(["T1A01", "T1A02"], now=1000.0)
    assert set(due) == {"T1A01", "T1A02"}
    store.close()


def test_recently_correct_question_is_not_due(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    store.record("T1A01", True, clock=lambda: 1000.0)  # box 2, 1-day interval
    assert "T1A01" not in store.due(["T1A01"], now=1000.0)
    store.close()


def test_overdue_question_becomes_due(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    store.record("T1A01", True, clock=lambda: 1000.0)  # box 2, 1-day interval
    later = 1000.0 + exam.BOX_INTERVALS[1] + 1
    assert "T1A01" in store.due(["T1A01"], now=later)
    store.close()


def test_due_orders_weakest_first(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    store.record("T1A02", True, clock=lambda: 0.0)   # box 2
    store.record("T1A02", True, clock=lambda: 0.0)   # box 3
    store.record("T1A01", False, clock=lambda: 0.0)  # box 1
    # Far enough in the future that both are due; the box-1 card must come first.
    due = store.due(["T1A01", "T1A02"], now=10**9)
    assert due[0] == "T1A01"
    store.close()


def test_store_defaults_under_data(tmp_path, monkeypatch):
    assert Path(exam.ExamStore.DEFAULT_DB_PATH).parent.name == "data"
    monkeypatch.chdir(tmp_path)
    store = exam.ExamStore()
    assert Path(exam.ExamStore.DEFAULT_DB_PATH).exists()
    store.close()


# ------------------------------------------------------------------- practice exam

def test_practice_exam_is_35_questions():
    pool = exam.load_pool()
    sheet = exam.build_exam(pool, rng=_seeded())
    assert len(sheet) == exam.EXAM_SIZE == 35


def test_practice_exam_matches_subelement_distribution():
    pool = exam.load_pool()
    sheet = exam.build_exam(pool, rng=_seeded())
    counts = {}
    for q in sheet:
        counts[q.subelement] = counts.get(q.subelement, 0) + 1
    assert counts == EXPECTED_BLUEPRINT


def test_practice_exam_draws_one_per_group():
    pool = exam.load_pool()
    sheet = exam.build_exam(pool, rng=_seeded())
    groups = [q.group for q in sheet]
    assert len(groups) == len(set(groups)) == 35


def test_practice_exam_has_no_duplicate_questions():
    pool = exam.load_pool()
    sheet = exam.build_exam(pool, rng=_seeded())
    ids = [q.id for q in sheet]
    assert len(ids) == len(set(ids))


def test_build_exam_is_reproducible_with_a_seed():
    pool = exam.load_pool()
    a = [q.id for q in exam.build_exam(pool, rng=_seeded())]
    b = [q.id for q in exam.build_exam(pool, rng=_seeded())]
    assert a == b


# ------------------------------------------------------------------- scoring

def _answer_sheet(sheet, correct_count):
    """Build an answer map that gets exactly correct_count questions right."""
    answers = {}
    for i, q in enumerate(sheet):
        if i < correct_count:
            answers[q.id] = q.answer
        else:
            answers[q.id] = "A" if q.answer != "A" else "B"  # a deliberate wrong pick
    return answers


def test_scoring_all_correct_passes():
    pool = exam.load_pool()
    sheet = exam.build_exam(pool, rng=_seeded())
    result = exam.score_exam(sheet, _answer_sheet(sheet, 35))
    assert result.correct == 35
    assert result.total == 35
    assert result.passed is True


def test_scoring_at_the_pass_mark_passes():
    pool = exam.load_pool()
    sheet = exam.build_exam(pool, rng=_seeded())
    result = exam.score_exam(sheet, _answer_sheet(sheet, exam.PASS_MARK))
    assert result.correct == 26
    assert result.passed is True


def test_scoring_below_the_pass_mark_fails():
    pool = exam.load_pool()
    sheet = exam.build_exam(pool, rng=_seeded())
    result = exam.score_exam(sheet, _answer_sheet(sheet, exam.PASS_MARK - 1))
    assert result.correct == 25
    assert result.passed is False


def test_pass_mark_is_26_of_35():
    assert exam.PASS_MARK == 26
    assert exam.EXAM_SIZE == 35


def test_scoring_detail_marks_each_question():
    pool = exam.load_pool()
    sheet = exam.build_exam(pool, rng=_seeded())
    result = exam.score_exam(sheet, _answer_sheet(sheet, 30))
    assert len(result.detail) == 35
    assert sum(1 for d in result.detail if d.is_correct) == 30
    # An unanswered question scores wrong, never silently correct.
    partial = exam.score_exam(sheet, {})
    assert partial.correct == 0
    assert partial.passed is False


def _seeded():
    import random
    return random.Random(1234)


# ------------------------------------------------------- practice-exam session (sim)

def test_sim_starts_empty_and_round_trips(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    assert store.sim() is None
    store.start_sim(["T1A01", "T2B02", "T3C03"])
    s = store.sim()
    assert s["question_ids"] == ["T1A01", "T2B02", "T3C03"]
    assert s["answers"] == {}


def test_sim_records_answers_in_order(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    store.start_sim(["T1A01", "T2B02"])
    store.record_sim_answer("T1A01", "B")
    assert store.sim()["answers"] == {"T1A01": "B"}
    store.record_sim_answer("T2B02", "C")
    assert store.sim()["answers"] == {"T1A01": "B", "T2B02": "C"}


def test_sim_refuses_an_answer_for_a_question_not_on_the_sheet(tmp_path):
    # A stray id must not be stored — the sim only scores the 35 it drew.
    store = exam.ExamStore(tmp_path / "exam.db")
    store.start_sim(["T1A01"])
    store.record_sim_answer("T9Z99", "A")
    assert store.sim()["answers"] == {}


def test_starting_a_sim_replaces_the_previous_one(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    store.start_sim(["T1A01"])
    store.record_sim_answer("T1A01", "A")
    store.start_sim(["T2B02", "T3C03"])
    s = store.sim()
    assert s["question_ids"] == ["T2B02", "T3C03"]
    assert s["answers"] == {}


def test_clear_sim_removes_the_session(tmp_path):
    store = exam.ExamStore(tmp_path / "exam.db")
    store.start_sim(["T1A01"])
    store.clear_sim()
    assert store.sim() is None


def test_sim_survives_a_reopen(tmp_path):
    # Power-loss durability: a sim in progress is still there after the store reopens.
    db = tmp_path / "exam.db"
    first = exam.ExamStore(db)
    first.start_sim(["T1A01", "T2B02"])
    first.record_sim_answer("T1A01", "D")
    first.close()
    second = exam.ExamStore(db)
    assert second.sim()["answers"] == {"T1A01": "D"}
