# ABOUTME: The post-lesson check: an optional scored stage after explain posing one new situation on
# ABOUTME: the lesson's concept — its loader rules, the stored answer, the route, and the screen.

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sigint101.app import create_app
from sigint101.events import read_events
from sigint101.hardware import RadioProbe
from sigint101.lessons import LessonError, RevealLockedError, load_lessons, parse_lesson
from sigint101.progress import CheckAlreadyAnswered, Progress
from tests.test_templates import APP_CSS, THEMES, _contrast, _theme_tokens

PACK_DIR = Path(__file__).resolve().parent.parent / "lesson_packs"

CHECK = {
    "prompt": "One stripe is twice as wide as its neighbours. Likeliest reason?",
    "choices": [
        {"key": "power", "label": "Twice the power", "why": "WHY_POWER more power is brightness."},
        {"key": "hd", "label": "HD sidebands", "why": "WHY_HD digital sidebands flank it."},
        {"key": "two", "label": "Two stations", "why": "WHY_TWO two would show two cores."},
    ],
    "answer": "hd",
}


def _pack(**overrides) -> dict:
    """A six-stage intro pack in Unit 1, with a check unless overridden."""
    data = {
        "id": 3,
        "slug": "check-fixture",
        "title": "Check Fixture",
        "type": "intro",
        "concept": "CONCEPT_marker width names the kind of signal",
        "hardware": [],
        "stages": {
            "frame": "FRAME_marker", "demo": "DEMO_marker", "try": "TRY_marker",
            "predict": "PREDICT_marker which way?", "observe": "OBSERVE_marker",
            "explain": "EXPLAIN_marker",
        },
        "choices": [{"key": "a", "label": "Wider"}, {"key": "b", "label": "Narrower"},
                    {"key": "c", "label": "Same"}],
        "answer": ["c"],
        "verdict": "VERDICT_marker same slice.",
        "check": CHECK,
    }
    data.update(overrides)
    return data


def _check(**overrides) -> dict:
    return {**CHECK, **overrides}


# --- the loader ---------------------------------------------------------------------------------

def test_a_pack_without_a_check_walks_exactly_as_before():
    lesson = parse_lesson(_pack(check=None))
    assert lesson.check is None
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    assert lesson.sealed_stages == ("observe", "explain")


def test_a_check_is_the_last_stage_and_sits_behind_the_predict_gate():
    lesson = parse_lesson(_pack())
    assert lesson.stage_order[-2:] == ("explain", "check")
    assert lesson.sealed_stages == ("observe", "explain", "check")


def test_a_consolidation_pack_takes_a_check_too():
    data = _pack(type="consolidation")
    data["stages"] = {"predict": "p", "operate": "o", "observe": "b", "explain": "e"}
    assert parse_lesson(data).stage_order[-1] == "check"


def test_the_check_parses_onto_the_lesson():
    check = parse_lesson(_pack()).check
    assert check["prompt"] == CHECK["prompt"]
    assert check["answer"] == "hd"
    assert check["choices"][1] == {"key": "hd", "label": "HD sidebands",
                                   "why": "WHY_HD digital sidebands flank it."}


def test_the_check_rides_the_sealed_reveal_only():
    lesson = parse_lesson(_pack())
    with pytest.raises(RevealLockedError):
        lesson.reveal(prediction_committed=False)
    revealed = lesson.reveal(prediction_committed=True)
    assert revealed["check"]["answer"] == "hd"
    assert revealed["stages"] == {"observe": "OBSERVE_marker", "explain": "EXPLAIN_marker"}


@pytest.mark.parametrize("check, complaint", [
    ("not a mapping", "mapping"),
    (_check(prompt=""), "prompt"),
    (_check(choices=CHECK["choices"][:1]), "two"),
    (_check(choices=[*CHECK["choices"][:2], {"key": "two", "label": "Two stations"}]), "why"),
    (_check(choices=[*CHECK["choices"][:2], {"key": "two", "label": "", "why": "w"}]), "label"),
    (_check(choices=[*CHECK["choices"][:2],
                     {"key": "two", "label": "Two", "why": "one\ntwo"}]), "one line"),
    (_check(answer="wide"), "not one of its choice keys"),
    (_check(answer=["hd"]), "single key"),
    (_check(answer=""), "answer"),
    (_check(choices=[*CHECK["choices"][:2], {"key": False, "label": "No", "why": "w"}]), "quote"),
])
def test_a_half_authored_check_is_refused(check, complaint):
    with pytest.raises(LessonError, match=complaint):
        parse_lesson(_pack(check=check))


def test_feedback_on_the_key_is_right_with_its_own_why():
    feedback = parse_lesson(_pack()).check_feedback("hd")
    assert feedback == {
        "correct": True,
        "chosen": {"label": "HD sidebands", "why": "WHY_HD digital sidebands flank it."},
        "key": {"label": "HD sidebands", "why": "WHY_HD digital sidebands flank it."},
    }


def test_feedback_on_a_distractor_is_not_yet_with_both_whys():
    feedback = parse_lesson(_pack()).check_feedback("power")
    assert feedback["correct"] is False
    assert feedback["chosen"]["why"].startswith("WHY_POWER")
    assert feedback["key"]["why"].startswith("WHY_HD")


# --- the stored answer --------------------------------------------------------------------------

def test_a_check_answer_is_stored_with_whether_it_was_right(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    assert prog.get_check(3) is None
    prog.record_check(3, " hd ", correct=True)
    assert prog.get_check(3) == {"choice": "hd", "correct": True}
    prog.close()


def test_a_check_answer_stands_once_given(tmp_path):
    # The feedback shows the key, so a second answer would only copy it.
    prog = Progress(tmp_path / "progress.db")
    prog.record_check(3, "power", correct=False)
    with pytest.raises(CheckAlreadyAnswered):
        prog.record_check(3, "hd", correct=True)
    assert prog.get_check(3) == {"choice": "power", "correct": False}
    prog.close()


def test_a_blank_check_answer_is_refused(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    with pytest.raises(ValueError, match="blank"):
        prog.record_check(3, "  ", correct=False)
    prog.close()


def test_a_check_answer_leaves_the_prediction_alone(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    prog.record_prediction(3, "c", correct=True)
    prog.record_check(3, "power", correct=False)
    assert prog.get_prediction(3) == "c"
    assert prog.get_correct(3) is True
    assert prog.get(3)["prediction"] == "c"
    prog.close()


# --- the route and the screen -------------------------------------------------------------------

def _client(tmp_path, *packs):
    pack_dir = tmp_path / "packs"
    pack_dir.mkdir(parents=True)
    for data in packs:
        (pack_dir / f"{data['id']:02d}-pack.yaml").write_text(yaml.safe_dump(data),
                                                               encoding="utf-8")
    probe = RadioProbe(False, "lsusb", "no RTL2832U on USB bus")
    return TestClient(create_app(pack_dir=pack_dir, data_dir=tmp_path, probe=lambda: probe))


def _predict(client, lesson_id=3, key="a"):
    client.post(f"/api/predict?lesson_id={lesson_id}", data={"prediction": key},
                follow_redirects=False)


def _answer(client, choice, lesson_id=3):
    return client.post(f"/api/check?lesson_id={lesson_id}", data={"choice": choice},
                       follow_redirects=False)


def _panel(html, stage):
    start = html.index(f'<section class="stage-panel" data-stage="{stage}"')
    return html[start:html.index("</section>", start)]


def test_the_check_is_refused_before_a_prediction(tmp_path):
    with _client(tmp_path, _pack()) as client:
        resp = _answer(client, "hd")
        assert resp.status_code == 400
        assert "predict" in resp.json()["reason"]
        assert client.app.state.progress.get_check(3) is None


def test_the_check_prompt_stays_sealed_until_a_prediction(tmp_path):
    with _client(tmp_path, _pack()) as client:
        html = client.get("/lesson/3").text
    assert CHECK["prompt"] not in html
    assert "WHY_HD" not in html


def test_a_seven_stage_rail_is_marked_long_so_its_gate_label_fits(tmp_path):
    # Seven pips leave the "Locked until you predict" label room only to wrap; the long rail
    # tightens it so both lines stay inside the 40px rail (render-checked at 800x480).
    with _client(tmp_path, _pack()) as client:
        assert '<nav class="rail rail--long"' in client.get("/lesson/3").text
    with _client(tmp_path / "plain", _pack(check=None)) as client:
        assert '<nav class="rail"' in client.get("/lesson/3").text


def test_answering_the_check_scores_it_and_reopens_on_the_feedback(tmp_path):
    with _client(tmp_path, _pack()) as client:
        _predict(client)
        resp = _answer(client, "power")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/lesson/3"
        assert client.app.state.progress.get_check(3) == {"choice": "power", "correct": False}
        assert client.app.state.progress.get_stage(3)["stage"] == "check"
        events = [e for e in read_events(tmp_path / "events.jsonl") if e["kind"] == "check"]
        assert events[-1]["payload"] == {"lesson": 3, "correct": False}


@pytest.mark.parametrize("choice", ["", "wide"])
def test_a_check_answer_must_be_one_of_its_chips(tmp_path, choice):
    with _client(tmp_path, _pack()) as client:
        _predict(client)
        assert _answer(client, choice).status_code == 400
        assert client.app.state.progress.get_check(3) is None


def test_a_second_check_answer_is_refused(tmp_path):
    with _client(tmp_path, _pack()) as client:
        _predict(client)
        _answer(client, "power")
        resp = _answer(client, "hd")
        assert resp.status_code == 409
        assert client.app.state.progress.get_check(3)["choice"] == "power"


def test_a_lesson_without_a_check_has_no_check_to_answer(tmp_path):
    with _client(tmp_path, _pack(check=None)) as client:
        _predict(client)
        assert _answer(client, "hd").status_code == 404
        stage = client.post("/api/stage?lesson_id=3", data={"stage": "check"})
        assert stage.status_code == 400


def test_before_the_answer_the_check_offers_chips_and_commit_but_no_complete(tmp_path):
    with _client(tmp_path, _pack()) as client:
        _predict(client)
        html = client.get("/lesson/3").text
    check = _panel(html, "check")
    assert CHECK["prompt"] in check
    assert 'action="/api/check?lesson_id=3"' in check
    assert check.count('name="choice"') == 3
    assert "Commit answer" in check
    assert "data-complete" not in html
    # The whys are the feedback: none reaches the page before the answer.
    assert "WHY_" not in html
    # The explain is no longer the last stage, so it steps on to the check.
    explain = _panel(html, "explain")
    assert 'data-nav="next"' in explain


def test_a_wrong_answer_shows_not_yet_both_whys_and_complete(tmp_path):
    with _client(tmp_path, _pack()) as client:
        _predict(client)
        _answer(client, "power")
        html = client.get("/lesson/3").text
    check = _panel(html, "check")
    assert "Not yet" in check
    assert "WHY_POWER" in check and "WHY_HD" in check
    assert "WHY_TWO" not in check
    assert "verdict--miss" in check
    assert "data-complete" in check
    assert "Commit answer" not in check
    assert 'data-stage="check" hidden' not in html      # it reopens on the check


def test_a_right_answer_shows_right_and_its_why_once(tmp_path):
    with _client(tmp_path, _pack()) as client:
        _predict(client)
        _answer(client, "hd")
        html = client.get("/lesson/3").text
    check = _panel(html, "check")
    assert "Right" in check and "Not yet" not in check
    assert check.count("WHY_HD") == 1
    assert "verdict--hit" in check
    assert "data-complete" in check


def test_a_lesson_without_a_check_still_completes_from_the_explain(tmp_path):
    with _client(tmp_path, _pack(check=None)) as client:
        _predict(client)
        html = client.get("/lesson/3").text
    assert 'data-stage="check"' not in html
    assert "data-complete" in _panel(html, "explain")


# --- the shipped Unit 1 checks ------------------------------------------------------------------

UNIT_1 = (1, 2, 3, 4)


def test_every_unit_1_lesson_carries_a_check():
    lessons = load_lessons(PACK_DIR)
    assert [i for i in UNIT_1 if lessons[i].check is None] == []


def test_the_unit_1_check_keys_are_spread_across_chip_positions():
    lessons = load_lessons(PACK_DIR)
    spots = []
    for i in UNIT_1:
        check = lessons[i].check
        spots.append([c["key"] for c in check["choices"]].index(check["answer"]))
    assert max(spots.count(spot) for spot in set(spots)) <= 2, spots


# --- the result tag reads in every theme --------------------------------------------------------

def _css_rule(selector):
    match = re.search(r"\n" + re.escape(selector) + r" \{(.*?)\}", APP_CSS.read_text(), re.S)
    return match.group(1) if match else ""


def _token(rule, prop):
    match = re.search(r"(?<![-\w])" + prop + r":\s*var\((--[\w-]+)\)", rule)
    return match.group(1) if match else None


def _tag_colours(theme, outcome):
    """The (ink, fill) the check's Right/Not yet tag paints in a theme, through the CSS cascade.

    Resolved through the theme's own tokens, so a colour outside its palette has no hex to find.
    """
    ink = fill = None
    for selector in (".verdict__tag", f".verdict--{outcome} .verdict__tag",
                     f'.app[data-theme="{theme}"] .verdict--{outcome} .verdict__tag'):
        rule = _css_rule(selector)
        ink = _token(rule, "color") or ink
        fill = _token(rule, "background") or fill
    tokens = _theme_tokens(theme)
    return tokens[ink], tokens[fill]


@pytest.mark.parametrize("outcome", ["hit", "miss"])
@pytest.mark.parametrize("theme", THEMES)
def test_the_check_result_tag_reads_at_4_5_to_1(theme, outcome):
    ink, fill = _tag_colours(theme, outcome)
    ratio = _contrast(ink, fill)
    assert ratio >= 4.5, f"{theme} {outcome} tag {ink} on {fill} is {ratio:.2f}:1"
