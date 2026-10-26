# ABOUTME: Unit tests for the lesson-pack loader, its per-type stage rules, and the
# ABOUTME: commit-before-reveal gate — a malformed or mis-ordered pack must fail loudly.

from pathlib import Path

import pytest

from sigint101.lessons import (
    DEFAULT_TYPE,
    REQUIRED_STAGES,
    LessonError,
    RevealLockedError,
    load_lessons,
    parse_lesson,
)


def _consolidation(**overrides) -> dict:
    """Build a minimal valid consolidation pack (the four-stage default shape)."""
    data = {
        "id": 1,
        "slug": "see-the-invisible",
        "title": "See the Invisible",
        "concept": "a radio turns the invisible RF world into something you can read",
        "stages": {"predict": "p", "operate": "o", "observe": "b", "explain": "e"},
    }
    data.update(overrides)
    return data


def _intro(**overrides) -> dict:
    """Build a minimal valid intro pack (the six-stage frame->explain shape)."""
    data = {
        "id": 2,
        "slug": "what-is-a-radio",
        "title": "What Is a Radio",
        "concept": "the vocabulary a beginner needs before the first real reception",
        "type": "intro",
        "stages": {
            "frame": "f",
            "demo": "d",
            "try": "t",
            "predict": "p",
            "observe": "b",
            "explain": "e",
        },
    }
    data.update(overrides)
    return data


def _write(path: Path, text: str) -> None:
    """Drop a YAML pack file on disk for the directory-loading tests."""
    path.write_text(text, encoding="utf-8")


# --- pack type and stage order ------------------------------------------------


def test_consolidation_pack_loads_with_the_four_stage_order():
    lesson = parse_lesson(_consolidation())
    assert lesson.type == "consolidation"
    assert lesson.stage_order == ("predict", "operate", "observe", "explain")
    for stage in REQUIRED_STAGES["consolidation"]:
        assert lesson.stages[stage]


def test_intro_pack_loads_with_the_six_stage_order():
    lesson = parse_lesson(_intro())
    assert lesson.type == "intro"
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage]


def test_a_pack_without_a_type_defaults_to_consolidation():
    data = _consolidation()
    assert "type" not in data
    lesson = parse_lesson(data)
    assert lesson.type == DEFAULT_TYPE == "consolidation"


def test_an_intro_pack_missing_a_required_stage_is_refused():
    # An intro lesson must frame/demo/try before it asks for a prediction; drop one and
    # the pack is half-defined, which must fail at load rather than mid-lesson.
    data = _intro()
    del data["stages"]["demo"]
    with pytest.raises(LessonError, match="stage 'demo' is required"):
        parse_lesson(data)


def test_a_consolidation_pack_missing_a_required_stage_is_refused():
    data = _consolidation()
    del data["stages"]["operate"]
    with pytest.raises(LessonError, match="stage 'operate' is required"):
        parse_lesson(data)


def test_an_unknown_type_is_refused():
    with pytest.raises(LessonError, match="unknown lesson type"):
        parse_lesson(_consolidation(type="masterclass"))


# --- commit-before-reveal gate ------------------------------------------------


def test_consolidation_reveal_is_sealed_until_a_prediction_is_committed():
    lesson = parse_lesson(
        _consolidation(
            hints=["listen wide first", "watch the noise floor", "it is near 100 MHz"],
            quiz=[{"q": "what band?", "a": "FM broadcast"}],
            artifact="waterfall.png",
        )
    )
    # Nothing past predict is available before the learner commits a guess.
    with pytest.raises(RevealLockedError):
        lesson.reveal(prediction_committed=False)
    # Once committed, the sealed material unlocks: later stages, hints, quiz, artifact.
    revealed = lesson.reveal(prediction_committed=True)
    assert revealed["stages"] == {"operate": "o", "observe": "b", "explain": "e"}
    assert "predict" not in revealed["stages"]
    assert revealed["hints"] == ["listen wide first", "watch the noise floor", "it is near 100 MHz"]
    assert revealed["quiz"] == [{"q": "what band?", "a": "FM broadcast"}]
    assert revealed["artifact"] == "waterfall.png"


def test_intro_reveal_is_sealed_until_a_prediction_is_committed():
    lesson = parse_lesson(
        _intro(hints=["start at the top of the dial"], quiz=[{"q": "?", "a": "!"}])
    )
    with pytest.raises(RevealLockedError):
        lesson.reveal(prediction_committed=False)
    revealed = lesson.reveal(prediction_committed=True)
    # Intro puts predict after frame/demo/try, so only observe and explain are sealed.
    assert revealed["stages"] == {"observe": "b", "explain": "e"}
    assert "frame" not in revealed["stages"]
    assert "try" not in revealed["stages"]
    assert revealed["hints"] == ["start at the top of the dial"]


def test_sealed_stages_are_everything_past_predict_for_each_type():
    assert parse_lesson(_consolidation()).sealed_stages == ("operate", "observe", "explain")
    assert parse_lesson(_intro()).sealed_stages == ("observe", "explain")


# --- lifted loader validation (behaviour carried over from elmer) -------------


def test_missing_required_field_raises():
    with pytest.raises(LessonError, match="missing required field"):
        parse_lesson({"id": 9})


def test_stages_must_be_a_mapping():
    with pytest.raises(LessonError, match="must be a mapping"):
        parse_lesson(_consolidation(stages=["predict", "operate"]))


def test_panels_default_to_none_declared():
    assert parse_lesson(_consolidation()).panels == []


def test_a_declared_panel_loads():
    assert parse_lesson(_consolidation(panels=["gain"])).panels == ["gain"]


def test_an_unknown_panel_refuses_to_load():
    # A pack asking for a panel the console does not have would render nothing, silently,
    # mid-lesson — refuse at load instead.
    with pytest.raises(LessonError, match="unknown panel"):
        parse_lesson(_consolidation(panels=["flux-capacitor"]))


def test_panels_must_be_a_list():
    # The message matters: a bare string would otherwise fail letter-by-letter with a
    # baffling "unknown panel 'g'".
    with pytest.raises(LessonError, match="must be a list"):
        parse_lesson(_consolidation(panels="gain"))


def test_hints_must_be_a_list():
    with pytest.raises(LessonError, match="must be a list"):
        parse_lesson(_consolidation(hints="not-a-list"))


def test_needs_hardware_reflects_the_hardware_field():
    assert parse_lesson(_consolidation()).needs_hardware is False
    assert parse_lesson(_consolidation(hardware=["rtlsdr"])).needs_hardware is True


# --- directory loading --------------------------------------------------------


def test_load_lessons_reads_both_pack_types_from_a_directory(tmp_path):
    _write(
        tmp_path / "01-intro.yaml",
        "id: 1\ntype: intro\nslug: a\ntitle: A\nconcept: c\n"
        "stages:\n  frame: f\n  demo: d\n  try: t\n  predict: p\n  observe: b\n  explain: e\n",
    )
    _write(
        tmp_path / "02-drill.yaml",
        "id: 2\nslug: b\ntitle: B\nconcept: c\n"
        "stages:\n  predict: p\n  operate: o\n  observe: b\n  explain: e\n",
    )
    lessons = load_lessons(tmp_path)
    assert set(lessons) == {1, 2}
    assert lessons[1].type == "intro"
    assert lessons[2].type == "consolidation"


def test_duplicate_lesson_ids_are_refused(tmp_path):
    pack = (
        "id: 1\nslug: a\ntitle: A\nconcept: c\n"
        "stages:\n  predict: p\n  operate: o\n  observe: b\n  explain: e\n"
    )
    _write(tmp_path / "a.yaml", pack)
    _write(tmp_path / "b.yaml", pack)
    with pytest.raises(LessonError, match="duplicate lesson id"):
        load_lessons(tmp_path)


def test_a_non_mapping_pack_file_is_refused(tmp_path):
    _write(tmp_path / "bad.yaml", "- just\n- a\n- list\n")
    with pytest.raises(LessonError, match="must be a mapping"):
        load_lessons(tmp_path)
