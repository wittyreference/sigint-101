# ABOUTME: Integration tests for the FastAPI console — its routes and the server-side
# ABOUTME: commit-before-reveal gate, driven through the TestClient against a fixture pack.

import re
from pathlib import Path

from fastapi.testclient import TestClient

from sigint101 import exam
from sigint101.app import create_app
from sigint101.hardware import RadioProbe
from sigint101.spectrum import Sweeper, SweepRow, validate_span

# The suite owns its lesson content: the real lesson_packs/ are authored in parallel, so a test
# that read them would go red on someone else's unfinished pack. These fixtures are stable.
FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"


def _present(detail="RTL2832U 0bda:2838 on USB bus"):
    """A probe result standing for a dongle physically on the bus."""
    return RadioProbe(True, "lsusb", detail)


def _absent(detail="no RTL2832U on USB bus"):
    """A probe result standing for an empty bus — the no-mock honest answer."""
    return RadioProbe(False, "lsusb", detail)


class _StubSweeper:
    """An app-boundary stand-in for the real rtl_power Sweeper.

    It holds real SweepRow objects and reports them as a running sweep would, so the spectrum and
    tune routes can be exercised with no dongle attached. This is a test double at the hardware
    boundary — never a fake baked into the product, which the no-mock invariant forbids.
    """

    def __init__(self, rows):
        self._rows = list(rows)
        self._started = False
        self.low, self.high, self.bin_size, self.gain = 88_000_000, 108_000_000, 125_000, None

    @property
    def running(self):
        return self._started

    def start(self):
        self._started = True

    def command(self):
        return ["rtl_power", "-f", f"{self.low}:{self.high}:{self.bin_size}"]

    def rows(self, limit=None):
        return self._rows if limit is None else self._rows[-limit:]

    def retune(self, low, high, bin_size=None):
        self.low, self.high, self.bin_size = validate_span(low, high, bin_size)

    def stop(self):
        self._started = False


def _rows():
    """A couple of real full-band sweeps, so as_dict() (floor/peak/SNR) is exercised end to end."""
    return [
        SweepRow(ts="2026-01-01 00:00:00", freq_low=88_000_000.0, freq_high=108_000_000.0,
                 freq_step=5_000_000.0, dbm=(-40.0, -38.0, -12.0, -41.0, -39.0)),
        SweepRow(ts="2026-01-01 00:00:01", freq_low=88_000_000.0, freq_high=108_000_000.0,
                 freq_step=5_000_000.0, dbm=(-41.0, -37.0, -11.0, -42.0, -40.0)),
    ]


def _client(tmp_path, *, probe=None, sweeper_factory=None):
    """Build a console against the fixture pack and a temp data dir, with injectable hardware."""
    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path,
                     probe=probe or _absent, sweeper_factory=sweeper_factory)
    return TestClient(app)


# --- home ---------------------------------------------------------------------


def test_home_renders_a_tile_per_unit(tmp_path):
    """Home is the unit grid now: six unit tiles that drill into a unit's lessons.

    Lessons no longer sit directly on the home grid — they live one level down, under
    their unit — so a lesson title must not appear on home; a unit title and a /unit link
    must.
    """
    with _client(tmp_path) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "tile" in resp.text
    # The six curriculum units, by name and by link target.
    assert "Instrument" in resp.text
    assert "Modulation" in resp.text
    assert "Decode" in resp.text
    assert 'href="/unit/1"' in resp.text
    assert 'href="/unit/6"' in resp.text
    # A lesson title lives under its unit, not on the home grid.
    assert "Fixture Gain and SNR" not in resp.text


def test_unit_page_lists_its_lessons(tmp_path):
    """A unit page shows that unit's loaded lessons, each linking to the lesson."""
    with _client(tmp_path) as client:
        resp = client.get("/unit/1")
    assert resp.status_code == 200
    assert "Instrument" in resp.text
    assert "Fixture Gain and SNR" in resp.text
    assert "Fixture Second Lesson" in resp.text
    assert 'href="/lesson/1"' in resp.text
    assert 'href="/lesson/2"' in resp.text


def test_unit_page_for_an_unknown_unit_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/unit/99")
    assert resp.status_code == 404


def test_unit_page_with_no_lessons_yet_is_honest(tmp_path):
    """A real unit with nothing built yet renders (no lock, D16) and says so plainly."""
    with _client(tmp_path) as client:
        resp = client.get("/unit/5")  # Decode — no fixture packs load into it
    assert resp.status_code == 200
    assert "Decode" in resp.text
    assert 'href="/lesson/' not in resp.text
    assert "No lessons here yet" in resp.text


def test_home_unit_tile_shows_aggregate_lesson_progress(tmp_path):
    """Unit 1 holds two loaded fixture lessons; finishing one reads as 1 of 2 done."""
    with _client(tmp_path) as client:
        before = client.get("/").text
        assert "0/2" in before  # Unit 1: two loaded lessons, none done
        client.post("/api/predict?lesson_id=1", data={"prediction": "a"},
                    headers={"content-type": "application/x-www-form-urlencoded"})
        client.post("/api/complete?lesson_id=1", data={},
                    headers={"content-type": "application/x-www-form-urlencoded"})
        after = client.get("/").text
    assert "1/2" in after


# --- commit-before-reveal (the whole point, enforced server-side) -------------


def test_a_lesson_seals_every_post_predict_stage_before_a_commit(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/lesson/1")
    assert resp.status_code == 200
    # The predict gate is shown...
    assert "What happens to the gap" in resp.text
    # ...but nothing past it is in the HTML at all — not merely hidden, absent.
    assert "OPERATE_SENTINEL" not in resp.text
    assert "OBSERVE_SENTINEL" not in resp.text
    assert "EXPLAIN_SENTINEL" not in resp.text


def test_recording_a_prediction_unseals_the_rest_of_the_lesson(tmp_path):
    with _client(tmp_path) as client:
        posted = client.post("/api/predict?lesson_id=1", data={"prediction": "a"},
                             follow_redirects=False)
        assert posted.status_code == 303
        resp = client.get("/lesson/1")
    assert "OPERATE_SENTINEL" in resp.text
    assert "OBSERVE_SENTINEL" in resp.text
    assert "EXPLAIN_SENTINEL" in resp.text


def test_a_blank_prediction_is_refused_fail_closed(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/predict?lesson_id=1", data={"prediction": "   "})
    assert resp.status_code == 400
    assert resp.json()["recorded"] is False
    assert "blank" in resp.json()["reason"]


def test_a_prediction_accepts_json_too(tmp_path):
    with _client(tmp_path) as client:
        posted = client.post("/api/predict?lesson_id=1", json={"prediction": "b"},
                             follow_redirects=False)
        assert posted.status_code == 303
        resp = client.get("/lesson/1")
    assert "OPERATE_SENTINEL" in resp.text


def test_predict_on_an_unknown_lesson_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/predict?lesson_id=999", data={"prediction": "a"})
    assert resp.status_code == 404


def test_opening_an_unknown_lesson_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/lesson/999")
    assert resp.status_code == 404


# --- complete -----------------------------------------------------------------


def test_complete_is_refused_without_a_prediction(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/complete?lesson_id=1")
    assert resp.status_code == 400
    assert resp.json()["completed"] is False
    assert "predict" in resp.json()["reason"]


def test_complete_succeeds_after_a_prediction(tmp_path):
    with _client(tmp_path) as client:
        client.post("/api/predict?lesson_id=1", data={"prediction": "b"},
                    follow_redirects=False)
        resp = client.post("/api/complete?lesson_id=1")
    assert resp.status_code == 200
    assert resp.json()["completed"] is True


def test_complete_on_an_unknown_lesson_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/complete?lesson_id=999")
    assert resp.status_code == 404


# --- spectrum (no-mock: absence is an answer, never a fake row) ---------------


def test_spectrum_reports_503_and_a_reason_when_the_sweeper_sees_no_radio(tmp_path):
    with _client(tmp_path,
                 sweeper_factory=lambda: Sweeper(radio_present=lambda: False)) as client:
        resp = client.get("/api/spectrum")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["rows"] == []        # never a fabricated sweep
    assert "RTL-SDR" in body["reason"]


def test_spectrum_default_sweeper_also_refuses_without_a_radio(tmp_path):
    # No sweeper_factory injected: the real build_sweeper runs and must still refuse, not fake.
    with _client(tmp_path, probe=_absent) as client:
        resp = client.get("/api/spectrum")
    assert resp.status_code == 503
    assert resp.json()["available"] is False


def test_spectrum_returns_real_rows_when_the_sweep_is_running(tmp_path):
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.get("/api/spectrum")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["freq_low"] == 88_000_000
    assert len(body["rows"]) == 2
    assert body["rows"][0]["dbm"] == [-40.0, -38.0, -12.0, -41.0, -39.0]
    assert "snr_db" in body["rows"][0]


# --- hardware (probe reported as data) ----------------------------------------


def test_hardware_reports_a_present_radio(tmp_path):
    with _client(tmp_path, probe=_present) as client:
        resp = client.get("/api/hardware")
    assert resp.status_code == 200
    body = resp.json()
    assert body["present"] is True
    assert body["method"] == "lsusb"
    assert "RTL2832U" in body["detail"]


def test_hardware_reports_an_absent_radio(tmp_path):
    with _client(tmp_path, probe=_absent) as client:
        resp = client.get("/api/hardware")
    assert resp.json()["present"] is False


# --- tune (fail-closed with the reason) ---------------------------------------


def test_tune_refuses_an_out_of_range_span_with_the_reason(tmp_path):
    with _client(tmp_path, probe=_present) as client:
        resp = client.post("/api/tune", json={"low": 10_000_000, "high": 20_000_000})
    assert resp.status_code == 400
    assert resp.json()["tuned"] is False
    assert "tunes" in resp.json()["reason"]  # "this radio tunes 24-1766 MHz ..."


def test_tune_without_a_radio_is_503(tmp_path):
    with _client(tmp_path, probe=_absent) as client:
        resp = client.post("/api/tune", json={"low": 100_000_000, "high": 104_000_000})
    assert resp.status_code == 503
    assert resp.json()["tuned"] is False


def test_tune_accepts_a_valid_span_when_a_radio_is_present(tmp_path):
    with _client(tmp_path, probe=_present,
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        resp = client.post("/api/tune", json={"low": 100_000_000, "high": 104_000_000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tuned"] is True
    assert body["low"] == 100_000_000
    assert body["high"] == 104_000_000


# --- exam trainer: one study card, commit-before-reveal (like the predict gate) ---------

# A known-good pool question to drive the answer/review endpoints with, independent of which card
# /exam happens to surface first (that ordering is the store's business, not these tests').
_Q = exam.load_pool().get("T1A01")


def test_exam_serves_a_study_card_with_tap_choices_and_no_answer(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/exam")
    assert resp.status_code == 200
    # It surfaces a real pool question (its id is in the eyebrow) with four tap choices.
    served = re.search(r"EXAM &middot; (T\w+)", resp.text).group(1)
    assert exam.load_pool().get(served) is not None
    assert resp.text.count('name="choice"') == 4
    # Commit-before-reveal: the correct answer is not marked until the learner has answered.
    assert "exam-reveal" not in resp.text


def test_exam_answering_records_and_reveals_the_result(tmp_path):
    with _client(tmp_path) as client:
        posted = client.post("/api/exam/answer?question_id=T1A01",
                             data={"choice": _Q.answer},
                             headers={"content-type": "application/x-www-form-urlencoded"},
                             follow_redirects=False)
        # The gate redirects to the review of the card just answered, with no JavaScript.
        assert posted.status_code == 303
        review = client.get(posted.headers["location"])
    assert review.status_code == 200
    assert "exam-reveal" in review.text
    # The correct option is marked, and a right answer reads as correct.
    assert "exam-chip--correct" in review.text
    assert "Correct" in review.text


def test_exam_a_wrong_answer_still_reveals_the_right_one(tmp_path):
    wrong = next(letter for letter in "ABCD" if letter != _Q.answer)
    with _client(tmp_path) as client:
        posted = client.post("/api/exam/answer?question_id=T1A01",
                             data={"choice": wrong},
                             headers={"content-type": "application/x-www-form-urlencoded"},
                             follow_redirects=False)
        review = client.get(posted.headers["location"])
    assert review.status_code == 200
    assert "exam-chip--correct" in review.text   # the right answer is always shown
    assert "exam-chip--chosen" in review.text     # and the learner's miss is marked


def test_exam_answer_for_an_unknown_question_is_404(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/exam/answer?question_id=NOPE99",
                           data={"choice": "A"},
                           headers={"content-type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 404
