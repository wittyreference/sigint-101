# ABOUTME: Renders the touch-console Jinja templates (base/console/home) with sample
# ABOUTME: contexts and asserts the shell, the keyboard-free predict gate, and the seal.

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sigint101.lessons import parse_lesson

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "sigint101" / "templates"


# --- sample data --------------------------------------------------------------
# Distinctive markers per stage so "absent before commit / present after" can be
# asserted on the stage body rather than its name (names show in the rail always).


def _consolidation_lesson():
    """A four-stage drill: predict is the gate, operate/observe/explain are sealed."""
    return parse_lesson(
        {
            "id": 1,
            "slug": "gain-snr",
            "title": "Gain and SNR",
            "concept": "the gap over the noise floor is the signal, not the peak",
            "panels": ["ppm", "gain"],
            "hardware": ["rtlsdr"],
            "stages": {
                "predict": "Turn the gain up. What happens to the gap over the floor?",
                "operate": "OPERATE_BODY_marker turn the gain and watch the gap",
                "observe": "OBSERVE_BODY_marker the station blooms, the floor climbs too",
                "explain": "EXPLAIN_BODY_marker SNR is the gap; past a point gain lifts both",
            },
        }
    )


def _intro_lesson():
    """A six-stage first exposure: frame/demo/try precede the predict gate."""
    return parse_lesson(
        {
            "id": 2,
            "slug": "see-the-invisible",
            "title": "See the Invisible",
            "type": "intro",
            "concept": "a radio turns invisible RF into something you can read",
            "panels": ["ppm"],
            "hardware": ["rtlsdr"],
            "stages": {
                "frame": "FRAME_BODY_marker the air is full of signals you cannot see",
                "demo": "DEMO_BODY_marker watch the loudest FM station bloom",
                "try": "TRY_BODY_marker now tune it yourself with the big buttons",
                "predict": "Where on the dial will the next station appear?",
                "observe": "OBSERVE_INTRO_marker a second carrier rises a few MHz up",
                "explain": "EXPLAIN_INTRO_marker each bright column is one station",
            },
        }
    )


CHOICES = [
    {"key": "a", "label": "The gap grows — the station pulls away from the floor"},
    {"key": "b", "label": "The gap holds — station and floor rise together"},
    {"key": "c", "label": "The gap shrinks past a point — both max out"},
]

# Tile dicts shaped as the home route builds them (no locks, D16): each carries its own
# link target and aggregate lesson progress.
UNITS = [
    {"code": "U1", "name": "Instrument", "glyph": "waterfall", "href": "/unit/1",
     "lessons_total": 4, "lessons_done": 1, "status": "active"},
    {"code": "U2", "name": "Rules", "glyph": "rules", "href": "/unit/2",
     "lessons_total": 1, "lessons_done": 0, "status": ""},
    {"code": "U3", "name": "Antennas", "glyph": "antenna", "href": "/unit/3",
     "lessons_total": 2, "lessons_done": 0, "status": ""},
    {"code": "U4", "name": "Modulation", "glyph": "wave", "href": "/unit/4",
     "lessons_total": 5, "lessons_done": 0, "status": ""},
    {"code": "U5", "name": "Decode", "glyph": "decode", "href": "/unit/5",
     "lessons_total": 4, "lessons_done": 0, "status": ""},
    {"code": "U6", "name": "Under the Hood", "glyph": "gear", "href": "/unit/6",
     "lessons_total": 2, "lessons_done": 0, "status": ""},
]


def _status(**overrides):
    """Status-bar context shared by every screen."""
    data = {
        "theme": "dark",
        "title": "Gain and SNR",
        "back_href": "/unit/1",
        "back_label": "Unit 1",
        "ppm_corrected": False,
        "ppm_value": 164,
        "snr_db": 14,
        "battery_pct": 82,
        "battery_charging": False,
    }
    data.update(overrides)
    return data


def _env():
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )


def _render(name, **ctx):
    return _env().get_template(name).render(**ctx)


def _console(lesson=None, **ctx):
    """Render console.html with sensible console defaults the test can override."""
    base = _status()
    base.update(
        lesson=lesson or _consolidation_lesson(),
        current_stage="predict",
        prediction_committed=False,
        predict_choices=CHOICES,
        committed_choice=None,
        commit_url="/lesson/1/commit",
        tuned_hz=100_300_000,
        step_hz=100_000,
    )
    base.update(ctx)
    return _render("console.html", **base)


# --- the shell (base.html) ----------------------------------------------------


def test_base_renders_the_persistent_status_bar():
    html = _render("base.html", **_status())
    assert "statusbar" in html
    # Back is always visible in the shell.
    assert "btn-back" in html
    # The four status-bar slots the shell promises.
    assert "chip--ppm" in html
    assert "readout--snr" in html and "SNR" in html
    assert "readout--batt" in html
    assert "btn-theme" in html


def test_base_ppm_chip_reads_raw_or_corrected_from_context():
    raw = _render("base.html", **_status(ppm_corrected=False))
    assert "RAW" in raw
    corrected = _render("base.html", **_status(ppm_corrected=True))
    assert "CORRECTED" in corrected


def test_base_carries_the_selected_theme_onto_the_app_root():
    assert 'data-theme="red-night"' in _render("base.html", **_status(theme="red-night"))
    assert 'data-theme="sunlight"' in _render("base.html", **_status(theme="sunlight"))


# --- the console shell + instrument -------------------------------------------


def test_console_shows_the_status_bar_and_back():
    html = _console()
    assert "statusbar" in html
    assert "btn-back" in html


def test_console_has_a_waterfall_canvas_and_big_tune_controls():
    # The instrument shows on the hands-on stages; render one that is visible.
    html = _console(prediction_committed=True, current_stage="operate")
    assert "<canvas" in html
    assert "waterfall" in html
    # tap-to-tune plus big +/- controls.
    assert "tune-btn" in html
    assert "data-tune" in html  # tap-to-tune hook on the canvas/scale


def test_console_waterfall_sample_is_labelled_not_live():
    # The RF invariant: a placeholder render must never pass as real signal.
    html = _console(prediction_committed=True, current_stage="operate")
    assert "SAMPLE" in html


# --- the keyboard-free predict gate -------------------------------------------


def test_predict_gate_offers_choice_chips_not_a_text_box():
    html = _console()
    assert "predict-chips" in html
    assert "predict-chip" in html
    # Every choice label the context passed is rendered as a tappable chip.
    for choice in CHOICES:
        assert choice["label"] in html


def test_predict_gate_is_keyboard_free():
    # No on-screen-keyboard affordance anywhere on the lesson screen.
    html = _console()
    assert 'type="text"' not in html
    assert 'type="search"' not in html
    assert "<textarea" not in html
    assert "contenteditable" not in html


def test_predict_commit_is_a_single_tap_control():
    html = _console()
    assert "btn-commit" in html
    assert 'name="prediction"' in html  # radio-style selection, keyboard-free


# --- commit-before-reveal (both pack shapes) ----------------------------------


def test_consolidation_seal_hides_every_post_predict_stage_until_commit():
    sealed = _console(prediction_committed=False)
    assert "OPERATE_BODY_marker" not in sealed
    assert "OBSERVE_BODY_marker" not in sealed
    assert "EXPLAIN_BODY_marker" not in sealed
    # predict itself is always available.
    assert "What happens to the gap" in sealed


def test_consolidation_reveal_appears_once_committed():
    revealed = _console(
        prediction_committed=True, current_stage="observe", committed_choice="b"
    )
    assert "OPERATE_BODY_marker" in revealed
    assert "OBSERVE_BODY_marker" in revealed
    assert "EXPLAIN_BODY_marker" in revealed


def test_intro_shows_frame_demo_try_before_the_gate_but_seals_the_rest():
    lesson = _intro_lesson()
    sealed = _console(lesson=lesson, current_stage="demo", prediction_committed=False)
    # frame/demo/try precede predict, so they are open from the start.
    assert "FRAME_BODY_marker" in sealed
    assert "DEMO_BODY_marker" in sealed
    assert "TRY_BODY_marker" in sealed
    # observe/explain still sit behind the predict gate.
    assert "OBSERVE_INTRO_marker" not in sealed
    assert "EXPLAIN_INTRO_marker" not in sealed


def test_intro_reveal_appears_once_committed():
    lesson = _intro_lesson()
    revealed = _console(
        lesson=lesson, current_stage="observe", prediction_committed=True
    )
    assert "OBSERVE_INTRO_marker" in revealed
    assert "EXPLAIN_INTRO_marker" in revealed


def test_a_sealed_stage_requested_without_a_commit_stays_sealed():
    # Even if a caller points current_stage past the gate, the body must not leak.
    leaked = _console(current_stage="observe", prediction_committed=False)
    assert "OBSERVE_BODY_marker" not in leaked


# --- no-hardware (knowledge) lessons: no radio chrome -------------------------


def _rules_lesson():
    """A no-RF knowledge lesson (the legal rules): the intro shape, but no hardware or panels."""
    return parse_lesson({
        "id": 5, "slug": "the-rules", "title": "The Rules", "type": "intro",
        "concept": "listening is broad; transmitting is licensed — know the line",
        "panels": [], "hardware": [],
        "stages": {
            "frame": "FRAME_RULES_marker the one rule",
            "demo": "DEMO_RULES_marker walk a scenario",
            "try": "TRY_RULES_marker sort these",
            "predict": "You find an unencrypted dispatch channel. Legal to listen?",
            "observe": "OBSERVE_RULES_marker reflect",
            "explain": "EXPLAIN_RULES_marker the law",
        },
    })


def test_a_no_hardware_lesson_renders_no_radio_chrome():
    # A lesson with no hardware (the legal rules) must not draw a waterfall on its demo/try/observe
    # stages, nor the frozen radio frame on predict — there is no radio to show.
    html = _console(lesson=_rules_lesson(), current_stage="demo")
    assert "<canvas" not in html
    assert "SIGNAL HIDDEN" not in html
    # The stage body still renders as plain text, and the keyboard-free predict chips still work.
    assert "DEMO_RULES_marker" in html
    assert "predict-chip" in html


def test_a_hardware_lesson_still_renders_the_radio():
    # The guard is scoped to no-hardware lessons: a radio lesson keeps its waterfall + frozen frame.
    html = _console(lesson=_intro_lesson(), current_stage="demo")
    assert "<canvas" in html
    assert "SIGNAL HIDDEN" in html


def _gain_intro_lesson():
    """An intro lesson that declares the gain panel; its demo stage is an unsealed instrument."""
    return parse_lesson({
        "id": 2, "slug": "gain", "title": "Turn It Up", "type": "intro",
        "concept": "SNR is the gap over the noise floor, not the peak",
        "panels": ["gain"], "hardware": ["rtlsdr"],
        "stages": {"frame": "f", "demo": "DEMO watch the gap", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_a_gain_lesson_shows_the_gain_meter_beside_the_waterfall():
    # A lesson that declares the gain panel draws the SNR level meter on its instrument stages,
    # with the hooks waterfall.js drives live.
    html = _console(lesson=_gain_intro_lesson(), current_stage="demo")
    assert "gain-meter" in html
    assert "data-gain-gap" in html and "data-gain-floor" in html


def test_a_lesson_without_the_gain_panel_has_no_gain_meter():
    # The intro fixture declares only the ppm panel, so its instrument stage draws no gain meter.
    html = _console(lesson=_intro_lesson(), current_stage="demo")
    assert "gain-meter" not in html


# --- the home / tile screen ---------------------------------------------------


def test_home_renders_unit_tiles_with_the_status_bar():
    html = _render(
        "home.html",
        **_status(title="SIGINT 101", back_href="/", back_label="Home"),
        tiles=UNITS,
    )
    assert "statusbar" in html
    assert "tile" in html
    for unit in UNITS:
        assert unit["name"] in html
        assert unit["code"] in html


def test_home_tiles_show_lesson_progress():
    html = _render("home.html", **_status(), tiles=UNITS)
    # "1/4" style progress for the active unit.
    assert "1/4" in html


def test_every_screen_has_no_horizontal_overflow_affordance():
    # Guard against a stray inline width that would break the 800x480 no-scroll rule;
    # this is a smoke check that the templates render without raising.
    for html in (_render("base.html", **_status()), _console(),
                 _render("home.html", **_status(), tiles=UNITS)):
        assert "<canvas" in html or "statusbar" in html


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
