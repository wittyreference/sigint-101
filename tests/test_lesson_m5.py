# ABOUTME: Tests M5's 12-card law sort, additive progress storage, /api/sort, /law, and D16.
# ABOUTME: Verifies 12 cards (>=3 depends), every card cited, non-lockout completion, and /law.

from __future__ import annotations

import html as html_lib
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sigint101.app import create_app
from sigint101.events import read_events
from sigint101.hardware import RadioProbe
from sigint101.law import LAW_CAUTION, LAW_SECTIONS
from sigint101.lessons import REQUIRED_STAGES, load_lesson_file
from sigint101.progress import Progress

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_M5 = REPO_ROOT / "lesson_packs" / "05-the-rules.yaml"


def _absent():
    return RadioProbe(False, "lsusb", "no RTL2832U on USB bus")


def _client(tmp_path):
    return TestClient(create_app(pack_dir=PACK_M5.parent, data_dir=tmp_path, probe=_absent))


# --- M5 12-card sort pack contract -------------------------------------------------------------


def test_m5_has_twelve_sort_cards_with_at_least_three_depends_and_every_card_cited():
    lesson = load_lesson_file(PACK_M5)
    assert lesson.stage_order == REQUIRED_STAGES["intro"]
    assert lesson.sort is not None
    assert len(lesson.sort) == 12

    answers = [card["answer"] for card in lesson.sort]
    assert answers.count("depends") >= 3
    assert answers.count("legal") >= 3
    assert answers.count("illegal") >= 3

    keys = [card["key"] for card in lesson.sort]
    assert len(set(keys)) == 12
    for card in lesson.sort:
        assert card["text"].strip()
        assert card["why"].strip() and "\n" not in card["why"]
        assert card["cite"].strip() and "\n" not in card["cite"]
        # Every citation must name a real statutory, regulatory, or case authority.
        assert re.search(r"\b(?:USC|CFR|F\.3d)\b", card["cite"]), card


def test_m5_sort_carries_over_m15_pager_line_and_joffe_wifi_payload_line():
    lesson = load_lesson_file(PACK_M5)
    cards = lesson.sort

    pager = [c for c in cards if "pager" in c["text"].lower() or "pocsag" in c["text"].lower()]
    assert pager, "expected a card carrying over M15's pager words rule"
    assert pager[0]["answer"] == "illegal"
    assert "2510" in pager[0]["cite"] and "2511" in pager[0]["cite"]

    joffe_cards = [c for c in cards if "joffe" in c["cite"].lower()]
    assert joffe_cards, "expected card(s) carrying over Joffe v. Google Wi-Fi payload line"
    assert any(c["answer"] == "illegal" and "payload" in (c["text"] + " " + c["why"]).lower()
               for c in joffe_cards)
    assert any("746 F.3d 920" in c["cite"] for c in joffe_cards)

    # Routine marine/aeronautical traffic sits under 18 USC 2511(2)(g)(ii)(IV), not (I) (distress).
    air_marine = [c for c in cards if c["key"] in ("aircraft-am", "adsb-ais")]
    assert len(air_marine) == 2
    assert all("2511(2)(g)(ii)(IV)" in c["cite"] for c in air_marine)


def test_m5_pack_cites_primary_sources_for_sort_authorities():
    raw = yaml.safe_load(PACK_M5.read_text(encoding="utf-8"))
    urls = [str(s.get("url", "")) for s in (raw.get("sources") or []) if isinstance(s, dict)]
    for expected in (
        "18/2510",
        "18/2511",
        "47/301",
        "47/605",
        "15.121",
        "15.247",
        "15.249",
        "97.5",
        "95.305",
        "95.1705",
        "11-17483",
    ):
        assert any(expected in u for u in urls), (
            f"missing primary source URL for {expected}: {urls}"
        )


# --- Additive sort_answers storage in progress.py ----------------------------------------------


def test_progress_sort_answers_table_stores_and_updates_without_breaking_existing_db(tmp_path):
    db = tmp_path / "progress.db"
    prog = Progress(db)
    assert prog.get_sort_answers(5) == {}
    prog.record_sort_answer(5, "aircraft-am", "illegal", correct=False)
    assert prog.get_sort_answers(5) == {"aircraft-am": {"choice": "illegal", "correct": False}}
    # Re-sorting updates in place (learning practice, never a lockout).
    prog.record_sort_answer(5, "aircraft-am", "legal", correct=True)
    prog.record_sort_answer(5, "pocsag-words", "illegal", correct=True)
    prog.close()

    reopened = Progress(db)
    assert reopened.get_sort_answers(5) == {
        "aircraft-am": {"choice": "legal", "correct": True},
        "pocsag-words": {"choice": "illegal", "correct": True},
    }
    with pytest.raises(ValueError, match="blank"):
        reopened.record_sort_answer(5, "", "legal", correct=True)
    with pytest.raises(ValueError, match="blank"):
        reopened.record_sort_answer(5, "aircraft-am", " ", correct=True)
    reopened.close()


# --- /api/sort route & D16 non-lockout completion ----------------------------------------------


def test_api_sort_records_answers_returns_feedback_and_validates_inputs(tmp_path):
    with _client(tmp_path) as client:
        ok = client.post(
            "/api/sort?lesson_id=5",
            json={"card": "pocsag-words", "choice": "illegal"},
            headers={"Accept": "application/json"},
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["recorded"] is True
        assert body["card"] == "pocsag-words"
        assert body["choice"] == "illegal"
        assert body["answer"] == "illegal"
        assert body["correct"] is True
        assert body["why"] and body["cite"]
        assert body["answered"] == 1 and body["score"] == 1 and body["total"] == 12

        bad_card = client.post("/api/sort?lesson_id=5", json={"card": "nope", "choice": "legal"})
        assert bad_card.status_code == 400
        bad_choice = client.post(
            "/api/sort?lesson_id=5", json={"card": "pocsag-words", "choice": "maybe"}
        )
        assert bad_choice.status_code == 400
        no_sort = client.post("/api/sort?lesson_id=1", json={"card": "x", "choice": "legal"})
        assert no_sort.status_code == 404

    events = [e for e in read_events(tmp_path / "events.jsonl") if e["kind"] == "sort_answer"]
    assert events and events[-1]["payload"]["card"] == "pocsag-words"


@pytest.mark.parametrize("mode", ["none", "all_wrong", "all_right"])
def test_complete_works_with_any_sort_score_d16(tmp_path, mode):
    """D16: law is knowledge, never a lockout — Complete succeeds regardless of sort score."""
    lesson = load_lesson_file(PACK_M5)
    with _client(tmp_path) as client:
        if mode != "none":
            for card in lesson.sort:
                wrong = "legal" if card["answer"] != "legal" else "illegal"
                choice = card["answer"] if mode == "all_right" else wrong
                resp = client.post(
                    "/api/sort?lesson_id=5",
                    json={"card": card["key"], "choice": choice},
                    headers={"Accept": "application/json"},
                )
                assert resp.status_code == 200
        pred = client.post(
            "/api/predict?lesson_id=5",
            data={"prediction": "illegal"},
            follow_redirects=False,
        )
        assert pred.status_code == 303
        done = client.post("/api/complete?lesson_id=5")
        assert done.status_code == 200
        assert done.json() == {"completed": True, "lesson": 5}


# --- Lesson 5 try stage UI & /law reading view -------------------------------------------------


def test_lesson_5_try_stage_renders_card_sort_and_law_link_keyboard_free(tmp_path):
    with _client(tmp_path) as client:
        html = client.get("/lesson/5").text
    assert 'data-sort-panel' in html
    assert 'data-sort-choice="legal"' in html
    assert 'data-sort-choice="illegal"' in html
    assert 'data-sort-choice="depends"' in html
    assert 'href="/law?back=/lesson/5"' in html
    assert "Show me the law" in html
    assert '/static/card-sort.js' in html
    # Keyboard-free: no text inputs anywhere on the lesson screen.
    assert 'type="text"' not in html
    assert "<textarea" not in html


def test_law_page_renders_every_authority_citation_caution_and_safe_back_link(tmp_path):
    with _client(tmp_path) as client:
        default_page = client.get("/law")
        assert default_page.status_code == 200
        html = default_page.text
        plain = html_lib.unescape(html)
        assert "the shape of the law, not legal advice; check your state" in plain.lower()
        assert "Specifics vary by country" in plain
        assert LAW_CAUTION in plain
        assert 'class="btn-back"' in html
        assert 'href="/"' in html
        for section in LAW_SECTIONS:
            assert section.title in plain
            assert section.cite in plain
            assert section.summary in plain

        from_m5 = client.get("/law?back=/lesson/5").text
        assert 'href="/lesson/5"' in from_m5

        # Never allow an external redirect in the Back link.
        unsafe = client.get("/law?back=https://evil.example/phish").text
        assert "evil.example" not in unsafe
        assert 'href="/"' in unsafe
