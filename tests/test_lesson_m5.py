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

    # F1 & F2: no card citing Joffe may be answered `legal`, and the payload card names the court.
    joffe_cards = [c for c in cards if "joffe" in c["cite"].lower()]
    assert joffe_cards, "expected card carrying over Joffe v. Google Wi-Fi payload line"
    assert all(c["answer"] != "legal" for c in joffe_cards), joffe_cards
    payload_card = next(c for c in joffe_cards if c["key"] == "open-wifi-payload")
    assert payload_card["answer"] == "illegal"
    assert "2510(16)" in payload_card["cite"]
    assert "2511(1)(a)" in payload_card["cite"]
    assert "746 F.3d 920" in payload_card["cite"]
    assert "9th Cir" in payload_card["cite"]
    assert "ninth circuit" in payload_card["why"].lower()
    assert "radio communication" in payload_card["why"].lower()

    # F1: wifi-beacons must not cite Joffe as authority making beacon surveys legal.
    beacon = next(c for c in cards if c["key"] == "wifi-beacons")
    assert "joffe" not in beacon["cite"].lower()

    # F9 & R3: 1 W needs certified/compliant gear (not the preset alone) under 15.247(b)(3).
    mesh = next(c for c in cards if c["key"] == "meshtastic-tx")
    assert "15.247(b)(3)" in mesh["why"] or "(b)(3)" in mesh["cite"]
    assert "(b)(3)" in mesh["cite"]
    assert re.search(r"\b(?:certified|compliant)\b", mesh["why"], re.I), mesh["why"]

    # R3: ham-tx-unlicensed must not over-generalize to "ham bands" (902-928 MHz is shared).
    ham = next(c for c in cards if c["key"] == "ham-tx-unlicensed")
    assert "ham bands" not in ham["why"].lower(), ham["why"]
    assert "amateur frequencies" not in ham["why"].lower(), ham["why"]
    assert re.search(r"\b(?:repeater|amateur station)\b", ham["why"], re.I), ham["why"]

    # Routine marine/aeronautical traffic sits under 18 USC 2511(2)(g)(ii)(IV), not (I) (distress).
    air_marine = [c for c in cards if c["key"] in ("aircraft-am", "adsb-ais")]
    assert len(air_marine) == 2
    assert all("2511(2)(g)(ii)(IV)" in c["cite"] for c in air_marine)


_SCENARIO_STOPWORDS = frozenset(
    "a an the to of on in it is you your with no and or all now that until do for at by from "
    "without while this its any".split()
)


def _scenario_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))
            if w not in _SCENARIO_STOPWORDS}


def test_m5_no_sort_card_restates_the_scored_predict():
    # F7: the try comes before the predict and every tap shows its verdict at once, so a card that
    # is the predict's own scenario hands the learner the scored answer one screen early.
    lesson = load_lesson_file(PACK_M5)
    predict = lesson.stages["predict"]
    assert "repeater" in predict.lower(), "the predict's setting moved; update this guard"
    predict_words = _scenario_words(predict)
    for card in lesson.sort:
        words = _scenario_words(card["text"])
        assert not words & {"repeater", "repeaters"}, (card["key"], card["text"])
        shared = predict_words & words
        assert len(shared) <= 3, (card["key"], sorted(shared))

    # The unlicensed amateur transmit case stays in the deck, on its primary authorities.
    ham = next(c for c in lesson.sort if c["key"] == "ham-tx-unlicensed")
    assert ham["answer"] == "illegal"
    assert "47 USC 301" in ham["cite"] and "97.5(a)" in ham["cite"]
    raw = yaml.safe_load(PACK_M5.read_text(encoding="utf-8"))
    by_title = {s["title"].split(" — ")[0]: s["url"] for s in raw["sources"]}
    assert by_title["47 U.S.C. 301"].startswith("https://www.govinfo.gov/content/pkg/USCODE-")
    assert by_title["47 U.S.C. 301"].endswith("sec301.htm")
    assert by_title["47 CFR 97.5"].startswith("https://www.ecfr.gov/current/title-47/")
    assert by_title["47 CFR 97.5"].endswith("section-97.5")


def test_m5_stages_agree_with_twelve_card_sort():
    # F3: observe and explain must not refer to "the four" as if the learner only saw four.
    lesson = load_lesson_file(PACK_M5)
    assert "the four" not in lesson.stages["observe"].lower()
    assert "the four" not in lesson.stages["explain"].lower()


def test_m5_pack_cites_primary_sources_for_sort_authorities():
    raw = yaml.safe_load(PACK_M5.read_text(encoding="utf-8"))
    sources = [s for s in (raw.get("sources") or []) if isinstance(s, dict)]
    titles = [str(s.get("title", "")) for s in sources]
    urls = [str(s.get("url", "")) for s in sources]
    # F9: pin the citation text in sources, with official government URLs (never LII or dead
    # uscode.house.gov prelim paths), rather than hard-coding brittle URL paths.
    assert not any("law.cornell.edu" in u for u in urls), urls
    assert not any("uscode.house.gov" in u for u in urls), urls
    assert all(u.startswith("https://") for u in urls), urls
    for cite_text in (
        "18 U.S.C. 2510",
        "18 U.S.C. 2511",
        "47 U.S.C. 301",
        "47 U.S.C. 605",
        "47 CFR 15.121",
        "47 CFR 15.247",
        "47 CFR 15.249",
        "47 CFR 97.5",
        "47 CFR 95.305",
        "47 CFR 95.1705",
        "746 F.3d 920",
    ):
        assert any(cite_text in t for t in titles), (
            f"missing primary source entry citing {cite_text}: {titles}"
        )


def test_law_sections_joffe_and_part15_are_exact_to_primary_sources():
    # F1: Joffe section keeps only the amended 2510(16) radio-communication holding, affirms the
    # denial of Google's motion to dismiss ("it did not decide liability"), puts 2014 cert denial
    # before settlement, limits reach to the Ninth Circuit, and notes silence on beacons.
    joffe = next(s for s in LAW_SECTIONS if s.id == "joffe-wifi")
    low = joffe.summary.lower()
    assert "nor 'readily accessible" not in low
    assert not re.search(r"\bheld\b[^.?!]*readily accessible", low)
    assert "finding no liability" not in low
    assert "decided no liability" not in low
    assert "affirmed the denial" in low and "motion to dismiss" in low
    assert "it did not decide liability" in low
    assert "2014" in low and "settled" in low and low.index("2014") < low.index("settled")
    assert "radio communication" in low and "2510(16)" in joffe.summary
    assert "ninth circuit" in low
    assert "beacon" in low and ("never decided" in low or "nothing about" in low)
    assert "746 F.3d 920" in joffe.cite

    # F9: Part 15 section cites 15.247(b)(3) for the 1 W limit and uses official gov domains.
    part15 = next(s for s in LAW_SECTIONS if s.id == "part15-ism")
    assert "(b)(3)" in part15.cite and "15.247(b)(3)" in part15.summary
    assert not any("law.cornell.edu" in s.url for s in LAW_SECTIONS)
    assert not any("uscode.house.gov" in s.url for s in LAW_SECTIONS)
    assert all(
        any(host in s.url for host in ("govinfo.gov", "ecfr.gov", "uscourts.gov"))
        for s in LAW_SECTIONS
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
    lesson = load_lesson_file(PACK_M5)
    with _client(tmp_path) as client:
        html = client.get("/lesson/5").text
    assert 'data-sort-panel' in html
    assert lesson.stages["try"] in html_lib.unescape(html)
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
