# ABOUTME: Renders the touch-console Jinja templates (base/console/home) with sample
# ABOUTME: contexts and asserts the shell, the keyboard-free predict gate, and the seal.

import math
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sigint101 import exam
from sigint101.lessons import load_lessons, parse_lesson
from sigint101.operate import INSTRUMENTS, instrument

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
        "ppm_value": 164,
        "snr_db": 14,
        "battery_pct": 82,
        "battery_charging": False,
    }
    data.update(overrides)
    return data


def test_status_bar_shows_unknown_battery_and_omits_unmeasured_snr():
    # A default of 100% read as a full battery on every screen. The unused SNR readout
    # looked like a second, empty battery, so give that space back when there is no reading.
    ctx = _status()
    del ctx["battery_pct"], ctx["snr_db"]
    html = _render("base.html", **ctx)
    batt = re.search(r'readout--batt.*?readout__value">(.*?)</span>', html, re.S).group(1)
    assert "100%" not in html
    assert batt == "&mdash;"
    assert "readout--snr" not in html


def test_status_bar_shows_a_real_battery_reading_when_one_is_given():
    html = _render("base.html", **_status(battery_pct=82, snr_db=14))
    assert "82%" in html
    assert "14 dB" in html


def test_the_waterfall_canvas_is_scaled_without_smoothing():
    # The canvas holds one pixel per rtl_power bin; the browser stretches it to the panel width.
    # Smoothed scaling would blend neighbouring bins into colours no bin measured, i.e. invented
    # signal, so the stretch must be nearest-neighbour.
    css = (TEMPLATES_DIR.parent / "static" / "app.css").read_text()
    rule = re.search(r"\n\.waterfall \{(.*?)\}", css, re.S).group(1)
    assert "image-rendering: pixelated" in rule


APP_CSS = TEMPLATES_DIR.parent / "static" / "app.css"
THEMES = ("dark", "sunlight", "red-night")


def _css_rule(selector):
    """The body of the first top-level rule for exactly this selector in app.css."""
    css = APP_CSS.read_text()
    match = re.search(r"\n" + re.escape(selector) + r" \{(.*?)\}", css, re.S)
    assert match, f"no {selector} rule in app.css"
    return match.group(1)


def _theme_tokens(theme):
    """Every custom property a theme defines, merged across its .app[data-theme] blocks."""
    css = APP_CSS.read_text()
    tokens = {}
    block_re = r'\.app\[data-theme="' + re.escape(theme) + r'"\] \{(.*?)\}'
    for block in re.findall(block_re, css, re.S):
        tokens.update(dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block)))
    return tokens


def _contrast(fg, bg):
    """WCAG 2 contrast ratio between two #rrggbb colours."""
    def luminance(hex_colour):
        channels = [int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _font_px(rule):
    return int(re.search(r"font-size:\s*(\d+)px", rule).group(1))


def test_the_no_radio_reason_is_page_text_over_the_canvas_not_canvas_pixels():
    # Text drawn into the canvas is squashed with the bitmap (~7 px tall on the panel), so the
    # reason rides in a DOM slot over the canvas instead; the canvas holds only real bins.
    html = _console(prediction_committed=True, current_stage="operate")
    wrap = re.search(r'class="waterfall-wrap">(.*?)</div>\s*</div>', html, re.S).group(1)
    assert "data-wf-reason" in wrap


def test_the_no_radio_reason_is_large_and_readable_in_every_theme():
    rule = _css_rule(".waterfall__reason")
    assert _font_px(rule) >= 18
    assert "var(--wf-ink)" in rule
    for theme in THEMES:
        tokens = _theme_tokens(theme)
        assert _contrast(tokens["--wf-ink"], tokens["--wf-bg"]) >= 4.5, theme


def test_the_waterfall_axis_reads_on_the_dark_band_in_every_theme():
    # The axis labels ride the waterfall, which stays dark in every theme. In sunlight the page's
    # dark-green ink measured about 2.1:1 there, so the axis takes the waterfall's own ink tokens.
    assert "var(--wf-axis)" in _css_rule(".waterfall__axis")
    assert "var(--wf-ink)" in _css_rule(".waterfall__axis b")
    for theme in THEMES:
        tokens = _theme_tokens(theme)
        for ink in ("--wf-axis", "--wf-ink"):
            assert _contrast(tokens[ink], tokens["--wf-bg"]) >= 4.5, (theme, ink)


def test_honest_empty_states_are_large_and_readable_in_every_theme():
    # "listening…" / "no radio" is the honest state a learner sees most: read it at a glance.
    for selector in (".decoder__empty", ".gain-meter__empty"):
        rule = _css_rule(selector)
        assert _font_px(rule) >= 18, selector
        assert "var(--fg-dim)" in rule, selector
    for theme in THEMES:
        tokens = _theme_tokens(theme)
        assert _contrast(tokens["--fg-dim"], tokens["--surface"]) >= 4.5, theme


def _hex_rgb(colour):
    return tuple(int(colour.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))


def test_red_night_paints_the_waterfall_from_a_red_only_ramp():
    # Red-night keeps night vision: the waterfall's levels are shades of red alone, never the
    # blue-to-yellow rainbow. Each stop has no more green or blue than a dim red tint.
    stops = _theme_tokens("red-night")["--wf-ramp"].split()
    assert len(stops) >= 2
    for stop in stops:
        r, g, b = _hex_rgb(stop)
        assert g == b and g <= r * 0.35, stop
    # Brighter is stronger: the ramp climbs from floor to peak.
    assert _hex_rgb(stops[-1])[0] > _hex_rgb(stops[0])[0]


def test_the_other_themes_keep_the_conventional_waterfall_map():
    assert "--wf-ramp" not in _theme_tokens("dark")
    assert "--wf-ramp" not in _theme_tokens("sunlight")


def _apt_lesson():
    return parse_lesson({
        "id": 24, "slug": "apt", "title": "APT", "type": "intro", "concept": "c",
        "panels": ["apt"], "hardware": ["rtl-sdr"],
        "stages": {"frame": "f", "demo": "d", "try": "t", "predict": "p", "observe": "o",
                   "explain": "e"},
    })


def test_a_decoded_picture_is_re_inked_red_in_red_night():
    # A decoded weather picture or SSTV frame is full colour; red-night maps its brightness onto red
    # alone with an SVG colour matrix (the technique the exam figures use), so no other hue shows.
    html = _console(lesson=_apt_lesson(), current_stage="demo")
    matrix = re.search(r'<filter id="gallery-ink-red"[^>]*>\s*<feColorMatrix type="matrix" '
                       r'values="([^"]+)"', html)
    assert matrix, "no red-night filter for the decoded picture"
    rows = [matrix.group(1).split()[i:i + 5] for i in range(0, 20, 5)]
    assert all(float(v) == 0 for v in rows[1] + rows[2])   # nothing reaches green or blue
    assert html.count('id="gallery-ink-red"') == 1
    css = APP_CSS.read_text()
    assert re.search(r'\.app\[data-theme="red-night"\] \.gallery__img \{[^}]*'
                     r'filter: url\(#gallery-ink-red\)', css)


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


def _eyebrow_for(html, stage):
    """The eyebrow text of one stage's panel (the first eyebrow after its section opens).

    Anchored on the panel's <section>: the rail's pips carry data-stage too.
    """
    m = re.search(
        r'<section[^>]*data-stage="' + re.escape(stage) + r'".*?stage-panel__eyebrow">(.*?)</div>',
        html,
        re.S,
    )
    return m.group(1) if m else ""


# --- the shell (base.html) ----------------------------------------------------


def test_base_renders_the_persistent_status_bar():
    html = _render("base.html", **_status())
    assert "statusbar" in html
    # Back is always visible in the shell.
    assert "btn-back" in html
    assert "readout--ppm" in html
    assert "data-status-radio" in html
    assert "/static/status-bar.js" in html
    assert "readout--snr" in html and "SNR" in html
    assert "readout--batt" in html
    assert "btn-theme" in html


def test_base_ppm_is_a_quiet_label_with_the_measured_sign():
    for value, label in ((None, "PPM —"), (0, "PPM +0"), (12, "PPM +12"), (-12.5, "PPM -12.5")):
        html = _render("base.html", **_status(ppm_value=value))
        span = re.search(r'<span class="readout readout--ppm"[^>]*>(.*?)</span>', html)
        assert span and span.group(1) == label
        assert "chip--ppm" not in html and "PPM RAW" not in html
    css = (TEMPLATES_DIR.parent / "static" / "app.css").read_text()
    style = re.search(r"\.readout--ppm\s*\{([^}]+)\}", css).group(1)
    assert "color: var(--fg-mute)" in style
    assert "border" not in style and "background" not in style and "cursor" not in style


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


# --- the explain verdict (a scored predict gate) ------------------------------


def _scored_lesson(**overrides):
    data = {
        "id": 1, "slug": "gain-snr", "title": "Gain and SNR", "type": "intro",
        "concept": "the gap over the noise floor is the signal",
        "hardware": [],
        "stages": {"frame": "f", "demo": "d", "try": "t",
                   "predict": "Turn the gain up. What happens to the gap over the floor?",
                   "observe": "o", "explain": "EXPLAIN_SCORED_marker"},
        "choices": CHOICES,
        "answer": ["b", "c"],
        "verdict": "VERDICT_marker the gap holds, then shrinks at the top.",
    }
    data.update(overrides)
    return parse_lesson(data)


def _verdict_block(html):
    m = re.search(r'data-stage="explain".*?(<div class="verdict.*?</div>\s*</div>)', html, re.S)
    return m.group(1) if m else ""


def test_explain_header_sets_your_call_beside_what_the_radio_does():
    # A predict about the instrument (a radio lesson) reveals what the radio does, without
    # repeating the lesson's one-line verdict sentence (the explain body already says why).
    html = _console(lesson=_scored_lesson(hardware=["rtl-sdr"]), prediction_committed=True,
                    current_stage="explain", committed_choice="a")
    block = _verdict_block(html)
    assert f"Your call: <b>{CHOICES[0]['label']}</b>" in block
    assert (f"What the radio does: <b>{CHOICES[1]['label']} or {CHOICES[2]['label']}</b>"
            in block)
    assert "VERDICT_marker" not in block
    assert "verdict__why" not in block
    # The header rides at the top of the explain's scrolling body, so a long eyebrow can never
    # squeeze the reading itself to nothing on the fixed 800x480 panel.
    assert re.search(r'<div class="stage-panel__body">\s*<div class="scroll-copy">\s*'
                     r'<div class="verdict', html)
    assert "EXPLAIN_SCORED_marker" in html


@pytest.mark.parametrize("lesson", [
    _scored_lesson(),                                            # no radio: the rules, theory
    _scored_lesson(hardware=["rtl-sdr"], predict_kind="knowledge"),  # a radio lesson's fact
])
def test_a_knowledge_predict_reveals_in_fact(lesson):
    # A predict about what is true (the law, what a detection proves) is not something the radio
    # does, so its reveal reads "In fact".
    block = _verdict_block(_console(lesson=lesson, prediction_committed=True,
                                    current_stage="explain", committed_choice="a"))
    assert f"Your call: <b>{CHOICES[0]['label']}</b>" in block
    assert f"In fact: <b>{CHOICES[1]['label']} or {CHOICES[2]['label']}</b>" in block
    assert "What the radio does" not in block


@pytest.mark.parametrize("choice", ["a", "c"])
def test_the_predict_reveal_never_scores_the_guess(choice):
    # A predict is a guess made before the teaching, so the reveal reads it as a call, not a
    # result: no RIGHT/MISSED tag and no hit/miss styling, whichever chip was committed. The
    # stored `correct` is item-analysis data for the author, never shown to the learner.
    block = _verdict_block(_console(lesson=_scored_lesson(), prediction_committed=True,
                                    current_stage="explain", committed_choice=choice))
    assert block
    for score_word in ("RIGHT", "MISSED", "Answer:", "verdict--hit", "verdict--miss",
                       "verdict__tag"):
        assert score_word not in block


def test_the_verdict_stays_sealed_until_a_commit():
    html = _console(lesson=_scored_lesson(), prediction_committed=False)
    assert "VERDICT_marker" not in html
    assert 'class="verdict' not in html


def test_an_unscored_lesson_renders_no_verdict_header():
    html = _console(lesson=_intro_lesson(), prediction_committed=True,
                    current_stage="explain", committed_choice="a")
    assert 'class="verdict' not in html


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


def test_stage_headings_match_hardware_vs_no_hardware_lessons():
    hw = _console(lesson=_intro_lesson(), prediction_committed=True, committed_choice="a")
    assert ">Watch it happen</h1>" in _section(hw, "demo")
    assert ">Your turn</h1>" in _section(hw, "try")
    assert ">Observe</h1>" in _section(hw, "observe")
    assert ">What happened</h1>" in _section(hw, "explain")

    no_hw = _console(lesson=_rules_lesson(), prediction_committed=True, committed_choice="a")
    assert ">The idea</h1>" in _section(no_hw, "demo")
    assert "Watch it happen" not in no_hw
    assert ">Think it through</h1>" in _section(no_hw, "try")
    assert "Your turn" not in no_hw
    assert ">Check your call</h1>" in _section(no_hw, "observe")
    assert ">Why it holds</h1>" in _section(no_hw, "explain")


@pytest.mark.parametrize("hardware", [["rtl-sdr"], []], ids=["hardware", "no-hardware"])
def test_observe_shows_the_learners_sealed_call_without_revealing_the_answer(hardware):
    lesson = _scored_lesson(hardware=hardware)
    html = _console(lesson=lesson, prediction_committed=True,
                    current_stage="observe", committed_choice="a")
    obs = _section(html, "observe")
    assert 'class="observe-call' in obs
    assert f"Your call: <b>{CHOICES[0]['label']}</b>" in obs
    # Observe reminds the learner of their own call without leaking the answer early.
    for reveal in (CHOICES[1]["label"], CHOICES[2]["label"], "What the radio does", "In fact"):
        assert reveal not in obs


def test_predict_gate_uses_lock_verb_consistently():
    sealed = _console(lesson=_scored_lesson(hardware=["rtl-sdr"]), prediction_committed=False,
                      current_stage="predict")
    assert '<span class="rail__gate">Locked until you predict</span>' in sealed
    predict = _section(sealed, "predict")
    assert ">Lock it in</button>" in predict
    assert "Commit prediction" not in predict
    assert 'data-ready-text="Choice set. Lock it in to unlock."' in predict

    opened = _console(lesson=_scored_lesson(hardware=["rtl-sdr"]), prediction_committed=True,
                      current_stage="predict", committed_choice="a")
    predict_open = _section(opened, "predict")
    assert "Locked in" in predict_open
    assert "Prediction committed" not in predict_open


def test_exam_trainer_and_practice_exam_are_named_apart():
    home = _render("home.html", title="SIGINT 101", tiles=[], exit_to_desktop=True,
                   exam_disabled=False)
    assert "Technician study" in home
    assert "Technician practice" not in home
    q = exam.Question(id="T0A01", subelement="T0", group="T0A", fcc_refs=(),
                      question="Q?", options={"A": "a", "B": "b", "C": "c", "D": "d"},
                      answer="A", figure=None)
    trainer = _render("exam.html", title="Exam Trainer", back_href="/", back_label="Home",
                      question=q, reviewed=False)
    assert "EXAM TRAINER" in trainer
    assert ">Practice exam</a>" in trainer
    assert 'data-ready-text="Choice set. Ready to commit."' in trainer


def _section(html, stage):
    """One stage panel's whole <section>, by its data-stage."""
    m = re.search(r'<section[^>]*data-stage="' + re.escape(stage) + r'".*?</section>', html, re.S)
    return m.group(0) if m else ""


@pytest.mark.parametrize("lesson_fn", [_intro_lesson, _rules_lesson])
def test_every_eyebrow_is_the_step_and_stage_only(lesson_fn):
    # The eyebrow once reprinted the whole concept in 13px caps on frame and explain, squeezing the
    # L11-L28 bodies to 7-82px (UX S0-2). It now reads "Step n of 6 · STAGE" on every stage, radio
    # lesson or not.
    lesson = lesson_fn()
    html = _console(lesson=lesson, current_stage="frame", prediction_committed=True)
    for n, stage in enumerate(lesson.stage_order, start=1):
        eyebrow = " ".join(_eyebrow_for(html, stage).split())
        assert eyebrow == f"Step {n} of {len(lesson.stage_order)} &middot; {stage.upper()}"


def test_the_concept_never_shows_before_the_commit():
    # On most lessons the concept states the predict answer (curriculum verify §6a), so nothing
    # before the gate may carry it — not the frame, not an eyebrow.
    lesson = _intro_lesson()
    html = _console(lesson=lesson, current_stage="frame", prediction_committed=False)
    assert lesson.concept not in html


def test_the_explain_stage_carries_the_concept_once_inside_its_scrolling_body():
    # After the reveal the concept is the lesson's takeaway: once, in the explain body that scrolls
    # (the Complete button stays pinned in the footer), after the explanation it sums up.
    lesson = _intro_lesson()
    html = _console(lesson=lesson, current_stage="explain", prediction_committed=True)
    assert html.count(lesson.concept) == 1
    explain = _section(html, "explain")
    body = re.search(r'<div class="stage-panel__body">(.*?)</div>', explain, re.S).group(1)
    assert body.index("EXPLAIN_INTRO_marker") < body.index(lesson.concept)
    assert "stage-foot" in explain and "data-complete" in explain


def test_every_stage_but_the_first_offers_a_previous_step():
    lesson = _intro_lesson()
    html = _console(lesson=lesson, current_stage="frame", prediction_committed=True)
    for stage in lesson.stage_order:
        assert ('data-nav="prev"' in _section(html, stage)) == (stage != "frame"), stage


def test_a_committed_predict_screen_steps_on_instead_of_committing_again():
    # Going back to predict after the commit shows the locked-in choice; Next carries on from it.
    html = _console(lesson=_intro_lesson(), current_stage="predict", prediction_committed=True,
                    committed_choice="a")
    predict = _section(html, "predict")
    assert 'data-nav="next"' in predict
    assert "btn-commit" not in predict


def test_only_a_radio_lessons_hands_on_stages_are_instrument_panels():
    # The instrument layout (title and eyebrow on one row, narration band under the radio) applies
    # to the stages that draw the radio, and to nothing else.
    html = _console(lesson=_intro_lesson(), current_stage="frame", prediction_committed=True)
    for stage in ("demo", "try", "observe"):
        assert "stage-panel--instrument" in _section(html, stage).split(">", 1)[0]
    for stage in ("frame", "predict", "explain"):
        assert "stage-panel--instrument" not in _section(html, stage).split(">", 1)[0]
    rules = _console(lesson=_rules_lesson(), current_stage="demo")
    assert "stage-panel--instrument" not in rules


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


def test_demo_keeps_the_live_readings_and_reserves_radio_controls_for_try():
    html = _console(lesson=_gain_intro_lesson(), current_stage="demo")
    demo = _section(html, "demo")
    assert "data-tune-step" not in demo
    assert "data-gain-step" not in demo
    assert not re.search(r"<canvas[^>]* data-tune(?=\s|>)", demo)
    assert "Tap to tune" not in demo
    assert 'class="waterfall"' in demo and "data-gain-value" in demo

    # M2's Try asks the learner to step the gain and watch how the gap changes.
    try_stage = _section(html, "try")
    assert re.search(r"<canvas[^>]* data-tune(?=\s|>)", try_stage)
    assert 'data-tune-step="-1"' in try_stage and 'data-tune-step="1"' in try_stage
    assert 'data-gain-step="-1"' in try_stage and 'data-gain-step="1"' in try_stage
    assert re.search(r"data-gain-value[^>]*>&mdash;<", try_stage)


def test_the_gain_stepper_buttons_are_full_touch_targets():
    rule = _css_rule(".gain-meter .gain-meter__btn")
    assert re.search(r"width:\s*72px", rule) and re.search(r"height:\s*72px", rule)


def test_a_lesson_without_the_gain_panel_has_no_gain_meter():
    # The intro fixture declares only the ppm panel, so its instrument stage draws no gain meter.
    html = _console(lesson=_intro_lesson(), current_stage="demo")
    assert "gain-meter" not in html


def _decode_intro_lesson():
    """An intro lesson whose instrument is the rtl_433 decoder (M10's), tuned to the ISM band."""
    return parse_lesson({
        "id": 10, "slug": "fsk", "title": "The Carrier That Shifts", "type": "intro",
        "concept": "FSK rides two frequencies you read with a decoder, not the eye",
        "panels": ["decode"], "hardware": ["rtl-sdr"],
        "tune": {"center_hz": 433_920_000, "span_hz": 600_000},
        "stages": {"frame": "f", "demo": "DEMO watch the decoder", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_a_decode_lesson_shows_the_decoder_not_a_waterfall():
    # The decode instrument replaces the waterfall: the panel decode.js fills is there, and no
    # waterfall canvas is drawn on the instrument stage.
    html = _console(lesson=_decode_intro_lesson(), current_stage="demo",
                    tuned_hz=433_920_000, tune_band={"low_hz": 433_620_000,
                                                      "high_hz": 434_220_000,
                                                      "center_hz": 433_920_000,
                                                      "step_hz": 100_000})
    assert "data-decode-panel" in html
    assert "data-decode-list" in html
    assert "rtl_433" in html
    assert "433.920" in html                 # the ISM band it listens on
    assert "canvas class=\"waterfall\"" not in html


def test_a_waterfall_lesson_has_no_decoder_panel():
    html = _console(lesson=_intro_lesson(), current_stage="demo")
    assert "data-decode-panel" not in html


def test_a_decode_lessons_predict_frame_seals_the_decoder():
    # The frozen predict frame on a decode lesson shows the sealed decoder, never a waterfall.
    html = _console(lesson=_decode_intro_lesson(), current_stage="predict",
                    prediction_committed=False, tuned_hz=433_920_000)
    assert "READS HIDDEN" in html
    assert "waterfall-wrap--frozen" not in html


def _lora_intro_lesson():
    """An intro lesson whose instrument is the LoRa radio (M11's), reading meshtasticd."""
    return parse_lesson({
        "id": 11, "slug": "lora", "title": "Below the Noise Floor", "type": "intro",
        "concept": "LoRa's chirp spreads a signal so it decodes below the noise floor",
        "panels": ["lora"], "hardware": ["lora"],
        "stages": {"frame": "f", "demo": "DEMO watch a packet land", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_a_lora_lesson_shows_the_lora_panel_not_a_waterfall():
    html = _console(lesson=_lora_intro_lesson(), current_stage="demo")
    assert "data-lora-panel" in html
    assert "data-lora-list" in html
    assert "meshtastic" in html
    assert "canvas class=\"waterfall\"" not in html
    assert "data-decode-panel" not in html


def test_a_lora_lessons_predict_frame_seals_the_lora_panel():
    html = _console(lesson=_lora_intro_lesson(), current_stage="predict",
                    prediction_committed=False)
    assert "PACKETS HIDDEN" in html
    assert "waterfall-wrap--frozen" not in html


def _rds_intro_lesson():
    """An intro lesson whose instrument is the RDS decoder (M12's), tuned to an FM station."""
    return parse_lesson({
        "id": 12, "slug": "rds", "title": "The Hidden Text", "type": "intro",
        "concept": "RDS rides data on a subcarrier the waterfall never shows",
        "panels": ["rds"], "hardware": ["rtl-sdr"],
        "tune": {"center_hz": 96_000_000, "span_hz": 400_000},
        "stages": {"frame": "f", "demo": "DEMO watch the text appear", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_an_rds_lesson_shows_the_station_card_not_a_waterfall():
    html = _console(lesson=_rds_intro_lesson(), current_stage="demo", tuned_hz=96_000_000)
    assert "data-rds-panel" in html
    assert "data-rds-ps" in html
    assert "redsea" in html
    assert "96.0" in html                    # the FM station it decodes
    assert "canvas class=\"waterfall\"" not in html


def test_an_rds_lessons_predict_frame_seals_the_station():
    html = _console(lesson=_rds_intro_lesson(), current_stage="predict",
                    prediction_committed=False, tuned_hz=96_000_000)
    assert "STATION DATA HIDDEN" in html
    assert "waterfall-wrap--frozen" not in html


def _adsb_intro_lesson():
    """An intro lesson whose instrument is the ADS-B radar (M13's), on 1090 MHz."""
    return parse_lesson({
        "id": 13, "slug": "adsb", "title": "The Sky Talks", "type": "intro",
        "concept": "aircraft broadcast their identity and position in the clear on 1090 MHz",
        "panels": ["adsb"], "hardware": ["rtl-sdr"],
        "tune": {"center_hz": 1_090_000_000, "span_hz": 2_000_000},
        "stages": {"frame": "f", "demo": "DEMO watch a contact appear", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_an_adsb_lesson_shows_the_radar_scope_not_a_waterfall():
    html = _console(lesson=_adsb_intro_lesson(), current_stage="demo", tuned_hz=1_090_000_000)
    assert "data-adsb-panel" in html
    assert "data-adsb-dots" in html
    assert "dump1090" in html
    assert "1090" in html
    assert "canvas class=\"waterfall\"" not in html
    assert "data-rds-panel" not in html


def test_an_adsb_lessons_predict_frame_seals_the_scope():
    html = _console(lesson=_adsb_intro_lesson(), current_stage="predict",
                    prediction_committed=False, tuned_hz=1_090_000_000)
    assert "SKY HIDDEN" in html
    assert "waterfall-wrap--frozen" not in html


def _aprs_intro_lesson():
    """An intro lesson whose instrument is the APRS decoder (M14's), on the 2m packet channel."""
    return parse_lesson({
        "id": 14, "slug": "aprs", "title": "The Return Address", "type": "intro",
        "concept": "APRS rides AX.25: a framed packet with a callsign, path and position",
        "panels": ["aprs"], "hardware": ["rtl-sdr"],
        "tune": {"center_hz": 144_390_000, "span_hz": 200_000},
        "stages": {"frame": "f", "demo": "DEMO watch the roster fill", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_an_aprs_lesson_shows_the_roster_not_a_waterfall():
    html = _console(lesson=_aprs_intro_lesson(), current_stage="demo", tuned_hz=144_390_000)
    assert "data-aprs-panel" in html
    assert "data-aprs-list" in html
    assert "multimon-ng" in html
    assert "144.390" in html
    assert "canvas class=\"waterfall\"" not in html
    assert "data-adsb-panel" not in html


def test_an_aprs_lessons_predict_frame_seals_the_roster():
    html = _console(lesson=_aprs_intro_lesson(), current_stage="predict",
                    prediction_committed=False, tuned_hz=144_390_000)
    assert "ROSTER HIDDEN" in html
    assert "waterfall-wrap--frozen" not in html


def _pocsag_intro_lesson():
    """An intro lesson whose instrument is the POCSAG decoder (M15's), on the pager band."""
    return parse_lesson({
        "id": 15, "slug": "pocsag", "title": "Not Meant for You", "type": "intro",
        "concept": "POCSAG sends pager messages in the clear, addressed to one cap code",
        "panels": ["pocsag"], "hardware": ["rtl-sdr"],
        "tune": {"center_hz": 929_612_500, "span_hz": 200_000},
        "stages": {"frame": "f", "demo": "DEMO watch the pages arrive", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_a_pocsag_lesson_shows_the_page_log_not_a_waterfall():
    html = _console(lesson=_pocsag_intro_lesson(), current_stage="demo", tuned_hz=929_612_500)
    assert "data-pocsag-panel" in html
    assert "data-pocsag-list" in html
    assert "multimon-ng" in html
    assert "929.6125" in html
    # MC's call for M15: framing live, text hidden; the instrument says so on its dial.
    assert "text withheld" in html
    assert "canvas class=\"waterfall\"" not in html
    assert "data-aprs-panel" not in html


def test_a_pocsag_lessons_predict_frame_seals_the_log():
    html = _console(lesson=_pocsag_intro_lesson(), current_stage="predict",
                    prediction_committed=False, tuned_hz=929_612_500)
    assert "PAGES HIDDEN" in html
    assert "POCSAG on 929.6125 MHz" in html
    assert "waterfall-wrap--frozen" not in html


def _sstv_intro_lesson():
    """An intro lesson whose instrument is the SSTV decoder (M25's), on the ISS downlink."""
    return parse_lesson({
        "id": 25, "slug": "sstv", "title": "A Picture in the Air", "type": "intro",
        "concept": "SSTV paints a picture in pitch",
        "panels": ["sstv"], "hardware": ["rtl-sdr"],
        "tune": {"center_hz": 437_550_000, "span_hz": 200_000},
        "stages": {"frame": "f", "demo": "d", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_an_sstv_lesson_names_the_frequency_its_pack_tunes():
    # The ISS SSTV downlink moved to 437.550 MHz in 2026; the dial and the waiting note read the
    # pack's band rather than a frequency baked into the template.
    demo = _console(lesson=_sstv_intro_lesson(), current_stage="demo", tuned_hz=437_550_000)
    assert "data-sstv-panel" in demo
    assert "437.550" in demo
    assert "145.8" not in demo
    sealed = _console(lesson=_sstv_intro_lesson(), current_stage="predict",
                      prediction_committed=False, tuned_hz=437_550_000)
    assert "SSTV on 437.550 MHz" in sealed


def _acars_intro_lesson():
    """An intro lesson whose instrument is the ACARS decoder (M16's), on the VHF ACARS channel."""
    return parse_lesson({
        "id": 16, "slug": "acars", "title": "What the Aircraft Says", "type": "intro",
        "concept": "ACARS is the airlines' VHF text link: typed messages, mostly in the clear",
        "panels": ["acars"], "hardware": ["rtl-sdr"],
        "tune": {"center_hz": 131_550_000, "span_hz": 200_000},
        "stages": {"frame": "f", "demo": "DEMO watch the messages arrive", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_an_acars_lesson_shows_the_message_log_not_a_waterfall():
    html = _console(lesson=_acars_intro_lesson(), current_stage="demo", tuned_hz=131_550_000)
    assert "data-acars-panel" in html
    assert "data-acars-list" in html
    assert "acarsdec" in html
    assert "131.550" in html
    assert "canvas class=\"waterfall\"" not in html
    assert "data-pocsag-panel" not in html


def test_an_acars_lessons_predict_frame_seals_the_log():
    html = _console(lesson=_acars_intro_lesson(), current_stage="predict",
                    prediction_committed=False, tuned_hz=131_550_000)
    assert "MESSAGES HIDDEN" in html
    assert "waterfall-wrap--frozen" not in html


def _flock_intro_lesson():
    """An intro lesson whose instrument is the Flock detector (M18's), on wlan1 monitor mode."""
    return parse_lesson({
        "id": 18, "slug": "flock-detector", "title": "The Camera's Fingerprint", "type": "intro",
        "concept": "A Flock ALPR camera leaks a WiFi fingerprint; a monitor radio recognises it",
        "panels": ["flock"], "hardware": ["wifi-monitor"],
        "stages": {"frame": "f", "demo": "DEMO watch the detections arrive", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_a_flock_lesson_shows_the_detections_list_not_a_waterfall():
    html = _console(lesson=_flock_intro_lesson(), current_stage="demo")
    assert "data-flock-panel" in html
    assert "data-flock-list" in html
    assert "Flock detector" in html
    assert "canvas class=\"waterfall\"" not in html
    assert "data-acars-panel" not in html


def test_a_flock_lessons_predict_frame_seals_the_list():
    html = _console(lesson=_flock_intro_lesson(), current_stage="predict",
                    prediction_committed=False)
    assert "DEVICES HIDDEN" in html
    assert "waterfall-wrap--frozen" not in html


def _flipper_intro_lesson():
    """An intro lesson whose instrument is the attached Flipper (M29's), read over its USB CLI."""
    return parse_lesson({
        "id": 29, "slug": "meet-flipper", "title": "Meet the Flipper", "type": "intro",
        "concept": "plugged into the nomad the Flipper gains a bench, not radios",
        "panels": ["flipper"], "hardware": ["flipper"],
        "stages": {"frame": "f", "demo": "DEMO watch the device answer", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_a_flipper_lesson_shows_the_device_panel_not_a_waterfall():
    html = _console(lesson=_flipper_intro_lesson(), current_stage="demo")
    demo = _section(html, "demo")
    assert "data-flipper-panel" in demo
    assert "canvas class=\"waterfall\"" not in html
    # The honest states every panel has: a badge that is not LIVE until the device answers, the
    # device card hidden, and the empty line at the readable size, never a device line up front.
    assert re.search(r"data-flipper-badge[^>]*>CHECKING<", demo)
    assert re.search(r"<div[^>]*data-flipper-card[^>]*\shidden", demo)
    assert re.search(r'class="decoder__empty" data-flipper-empty', demo)
    # Back a step rides the fixed dial beside Next, as on every other panel's stages.
    assert 'data-nav="prev"' in demo and 'data-nav="next"' in demo


def test_a_flipper_lessons_predict_frame_seals_the_device():
    html = _console(lesson=_flipper_intro_lesson(), current_stage="predict",
                    prediction_committed=False)
    assert "DEVICE HIDDEN" in html
    assert "data-flipper-panel" not in _section(html, "predict")
    assert "waterfall-wrap--frozen" not in html


def test_the_flipper_panel_is_the_shared_macro_not_lesson_markup():
    # The rack draws the same panel a lesson does, so its markup lives once, in _panels.html.
    assert "{% macro flipper() %}" in (TEMPLATES_DIR / "_panels.html").read_text()
    console = (TEMPLATES_DIR / "console.html").read_text()
    assert "{{ panels.flipper() }}" in console
    assert "data-flipper-panel" not in console


def test_the_flipper_device_card_reads_at_the_body_text_floor():
    for selector in (".flipper__line", ".flipper__radio"):
        assert _font_px(_css_rule(selector)) >= 18, selector
    # The card is a flex column, which would beat the UA [hidden] rule and show an empty card.
    assert "display: none" in _css_rule(".flipper[hidden]")


def _provenance_lesson():
    """An intro lesson that declares the provenance panel (M3's, deaf-vs-empty)."""
    return parse_lesson({
        "id": 3, "slug": "baseline", "title": "Know Your Instrument", "type": "intro",
        "concept": "a deaf radio looks like a quiet band; only a baseline catches it",
        "panels": ["provenance"], "hardware": ["rtlsdr"],
        "stages": {"frame": "f", "demo": "DEMO watch the baseline", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_a_provenance_lesson_shows_the_provenance_panel():
    html = _console(lesson=_provenance_lesson(), current_stage="demo")
    assert "prov-panel" in html
    assert "data-prov-verdict" in html and "Capture baseline" in html


def test_a_lesson_without_provenance_has_no_provenance_panel():
    html = _console(lesson=_intro_lesson(), current_stage="demo")
    assert "prov-panel" not in html


def test_a_ppm_lesson_shows_the_ppm_panel():
    # The intro fixture declares the ppm panel (M4's), so its instrument stage draws it.
    html = _console(lesson=_intro_lesson(), current_stage="demo")
    assert "ppm-panel" in html
    assert "Measure ppm" in html and "data-ppm-measure" in html


def test_a_lesson_without_ppm_has_no_ppm_panel():
    # The gain fixture declares only the gain panel, so there is no ppm panel on its stages.
    html = _console(lesson=_gain_intro_lesson(), current_stage="demo")
    assert "ppm-panel" not in html


# --- per-lesson tune band -----------------------------------------------------


def _tuned_lesson():
    """A hardware consolidation lesson that declares its own band (OOK at 433.92 MHz)."""
    return parse_lesson({
        "id": 9, "slug": "ook", "title": "On-Off Keying",
        "concept": "a carrier switched fully on and off is the simplest code of all",
        "hardware": ["rtlsdr"],
        "tune": {"center_hz": 433_920_000, "span_hz": 2_000_000, "step_hz": 20_000},
        "stages": {
            "predict": "Does the carrier vanish between pulses, or just dim?",
            "operate": "OP tune to the band and key the remote",
            "observe": "OB the line blinks fully out between pulses",
            "explain": "EX that full on/off is on-off keying",
        },
    })


def test_a_tuned_lesson_points_the_instrument_dial_at_its_band():
    lesson = _tuned_lesson()
    html = _console(lesson=lesson, current_stage="operate", prediction_committed=True,
                    tuned_hz=lesson.tune["center_hz"], step_hz=lesson.tune["step_hz"],
                    tune_band=lesson.tune)
    assert "433.920" in html                       # the dial reads the band centre
    assert "20 kHz" in html                         # the pack's finer tune step
    # The live waterfall carries the band edges so the console can retune the sweep to it.
    assert 'data-tune-low="432920000"' in html
    assert 'data-tune-high="434920000"' in html


def test_an_untuned_lesson_carries_no_band_to_retune_to():
    # The FM-band lessons declare no band; the console must emit no retune edges for them.
    html = _console(lesson=_consolidation_lesson(), current_stage="operate",
                    prediction_committed=True)
    assert "data-tune-low" not in html


def _live_instrument(html):
    """The live (operate) instrument's markup: its waterfall wrap and tune strip."""
    return re.search(r'<section class="stage-panel[^"]*" data-stage="operate".*?'
                     r'<div class="instrument(?: instrument--gain)?">(.*?)'
                     r'<div class="scroll-frame">\s*<div class="stage-panel__body">', html,
                     re.S).group(1)


def test_a_tuned_lesson_names_its_bin_size_and_step_for_the_live_tune():
    lesson = parse_lesson({
        "id": 6, "slug": "res", "title": "Res", "concept": "c", "hardware": ["rtlsdr"],
        "tune": {"center_hz": 1_090_000_000, "span_hz": 2_000_000, "step_hz": 1_000_000,
                 "bin_hz": 31_250},
        "stages": {"predict": "p", "operate": "o", "observe": "ob", "explain": "e"},
    })
    html = _live_instrument(_console(
        lesson=lesson, current_stage="operate", prediction_committed=True,
        tuned_hz=lesson.tune["center_hz"], step_hz=lesson.tune["step_hz"],
        tune_band=lesson.tune))
    assert 'data-tune-bin="31250"' in html
    assert 'data-step-hz="1000000"' in html


def test_the_tune_strip_steps_by_the_default_when_a_lesson_names_none():
    html = _live_instrument(_console(lesson=_consolidation_lesson(), current_stage="operate",
                                     prediction_committed=True))
    assert 'data-step-hz="100000"' in html
    assert "data-tune-bin" not in html


def test_the_dial_is_not_shown_as_tuned_until_the_radio_confirms_it():
    # The readout only reads as tuned once the server has confirmed a span; until then it is the
    # lesson's target, marked pending, so a dead radio never looks tuned.
    html = _live_instrument(_console(current_stage="operate", prediction_committed=True))
    assert "tune__freq--pending" in html


def test_the_instrument_axis_never_labels_a_span_nobody_tuned():
    # The default sweep is 88-108 MHz; an axis drawn as dial +/- 1.3 MHz labelled a span that was
    # never swept. Until the radio reports its span the axis carries no numbers.
    html = _live_instrument(_console(lesson=_consolidation_lesson(), current_stage="operate",
                                     prediction_committed=True))
    axis = re.search(r'class="waterfall__axis"[^>]*>(.*?)</div>', html, re.S).group(1)
    assert not re.search(r"\d", axis)


def test_a_tuned_lesson_axis_starts_on_its_own_band_edges():
    lesson = _tuned_lesson()
    html = _live_instrument(_console(
        lesson=lesson, current_stage="operate", prediction_committed=True,
        tuned_hz=lesson.tune["center_hz"], step_hz=lesson.tune["step_hz"],
        tune_band=lesson.tune))
    axis = re.search(r'class="waterfall__axis"[^>]*>(.*?)</div>', html, re.S).group(1)
    assert "432.9" in axis and "434.9" in axis


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


def test_home_offers_the_licence_exam_trainer_apart_from_the_units():
    """A full-width call-to-action under the grid lands on /exam — its own affordance,

    visually set apart from the six curriculum unit tiles (it is a cross-cutting track).
    """
    html = _render("home.html", **_status(), tiles=UNITS)
    assert 'class="exam-cta"' in html
    assert 'href="/exam"' in html
    assert "Licence" in html
    # It is not dressed as a unit tile.
    assert 'class="tile exam-cta"' not in html


def test_home_puts_the_exam_and_operator_bars_side_by_side_in_one_row():
    # MC's call 2026-11-02 ("Bars side by side"): the two cross-cutting tracks share one row under
    # the grid, so the unit tiles keep two full rows on the 800x480 screen instead of one.
    html = _render("home.html", **_status(), tiles=UNITS)
    row = re.search(r'<div class="home-tracks">(.*?</a>.*?</a>)\s*</div>', html, re.S)
    assert row, "the two track bars sit in one .home-tracks row"
    inside = row.group(1)
    assert 'href="/exam"' in inside and 'href="/operate"' in inside


def test_base_console_and_home_templates_render_with_canvas_or_statusbar():
    for html in (_render("base.html", **_status()), _console(),
                 _render("home.html", **_status(), tiles=UNITS)):
        assert "<canvas" in html or "statusbar" in html


# --- scored practice exam (sim card + results) --------------------------------


def _exam_ctx(**over):
    ctx = _status()
    ctx.update(over)
    return ctx


def test_exam_sim_card_shows_progress_and_running_score():
    q = exam.load_pool().get("T1A01")
    html = _render("exam_sim.html", **_exam_ctx(
        title="Practice Exam", back_href="/exam", back_label="Exam",
        question=q, reviewed=False, position=3, total=35, score=2, answered=2, pass_mark=26))
    assert "Question 3 of 35" in html
    assert "2 right of 2 answered" in html
    assert html.count('name="choice"') == 4
    assert "/api/exam/sim/answer" in html
    assert "exam-reveal" not in html          # commit-before-reveal on the sim too


def test_exam_sim_review_reveals_the_answer_and_points_to_results_on_the_last():
    q = exam.load_pool().get("T1A01")
    html = _render("exam_sim.html", **_exam_ctx(
        title="Practice Exam", back_href="/exam", back_label="Exam",
        question=q, reviewed=True, chosen=q.answer, correct=True,
        position=35, total=35, score=34, answered=35, pass_mark=26,
        next_href="/exam/sim/results", last=True))
    assert "exam-chip--correct" in html
    assert 'href="/exam/sim/results"' in html
    assert "See results" in html


@pytest.mark.parametrize("template", ["exam.html", "exam_sim.html"])
def test_a_figure_question_shows_its_diagram_behind_a_tap(template):
    # T6C02 refers to Figure T-1: the card offers a tap that opens the real diagram full-screen
    # (no JavaScript: a :target overlay), and never the old "not shown here yet" apology.
    q = exam.load_pool().get("T6C02")
    html = _render(template, **_exam_ctx(
        title="Exam", back_href="/", back_label="Home", question=q, reviewed=False,
        position=1, total=35, score=0, answered=0, pass_mark=26))
    assert 'href="#figure-T-1"' in html
    assert 'id="figure-T-1"' in html
    assert 'src="/static/exam/T-1.jpg"' in html
    assert "not shown here yet" not in html


def _pinned(html, button):
    """True when `button` sits in the exam's pinned footer, after the scrolling body that holds
    the question and the chips — so a long question can never push it off the 800x480 screen."""
    body = html.index('class="exam__body"')
    foot = html.index('class="exam__foot')
    return body < html.index('class="exam__question"') < foot and html.index(button) > foot


@pytest.mark.parametrize("template", ["exam.html", "exam_sim.html"])
def test_exam_card_pins_commit_below_a_scrolling_body(template):
    q = exam.load_pool().get("T1D01")
    html = _render(template, **_exam_ctx(
        title="Exam", back_href="/", back_label="Home", question=q, reviewed=False,
        position=1, total=35, score=0, answered=0, pass_mark=26))
    assert _pinned(html, "Commit answer")
    assert html.index('name="choice"') < html.index('class="exam__foot')
    # Commit lives outside the form's markup, so it names the form it submits.
    assert 'form="exam-answer"' in html and 'id="exam-answer"' in html


@pytest.mark.parametrize("template", ["exam.html", "exam_sim.html"])
def test_exam_review_pins_next_below_a_scrolling_body(template):
    q = exam.load_pool().get("T1D01")
    html = _render(template, **_exam_ctx(
        title="Exam", back_href="/", back_label="Home", question=q, reviewed=True,
        chosen="A", correct=False, position=1, total=35, score=0, answered=1, pass_mark=26,
        next_href="/exam/sim", last=False))
    assert _pinned(html, "exam-reveal")
    assert 'class="exam__question"' in html          # the question is never dropped on review


def test_exam_study_card_offers_the_practice_exam_from_its_footer():
    q = exam.load_pool().get("T1A01")
    html = _render("exam.html", **_exam_ctx(
        title="Exam", back_href="/", back_label="Home", question=q, reviewed=False))
    assert _pinned(html, 'href="/exam/sim"')


def test_exam_sim_card_offers_skip_while_another_question_waits():
    q = exam.load_pool().get("T1A01")
    html = _render("exam_sim.html", **_exam_ctx(
        title="Practice Exam", back_href="/exam", back_label="Exam", question=q, reviewed=False,
        position=4, total=35, score=1, answered=2, pass_mark=26, skipped=1, can_skip=True))
    # Skip submits the same form to the skip endpoint, without needing a choice tapped first.
    assert 'formaction="/api/exam/sim/skip?question_id=T1A01"' in html
    assert "formnovalidate" in html
    assert _pinned(html, ">Skip<")
    assert "1 skipped" in html


def test_exam_sim_card_has_no_skip_on_the_last_question_left():
    q = exam.load_pool().get("T1A01")
    html = _render("exam_sim.html", **_exam_ctx(
        title="Practice Exam", back_href="/exam", back_label="Exam", question=q, reviewed=False,
        position=1, total=35, score=30, answered=34, pass_mark=26, skipped=1, can_skip=False))
    assert "/api/exam/sim/skip" not in html


def test_a_question_without_a_figure_offers_no_diagram():
    q = exam.load_pool().get("T1A01")
    html = _render("exam.html", **_exam_ctx(
        title="Exam", back_href="/", back_label="Home", question=q, reviewed=False))
    assert "exam-figure" not in html


def test_exam_results_shows_verdict_passline_and_subelement_breakdown():
    result = exam.ExamResult(total=35, correct=30, passed=True, detail=())
    breakdown = [{"subelement": "T1", "correct": 6, "total": 6},
                 {"subelement": "T5", "correct": 2, "total": 4}]
    html = _render("exam_results.html", **_exam_ctx(
        title="Practice Exam", back_href="/", back_label="Home",
        result=result, pass_mark=26, total=35, pct=86, pass_pct=74, breakdown=breakdown))
    assert "exam-results--pass" in html and "PASS" in html
    assert "30 of 35 correct" in html
    assert "Pass mark: 26 of 35 (74%)" in html
    assert "By subelement" in html
    assert "T1" in html and "6/6" in html
    assert "exam-results__row--weak" in html      # T5 was 2/4, flagged for study


def test_exam_results_not_passed_reads_not_yet():
    result = exam.ExamResult(total=35, correct=20, passed=False, detail=())
    html = _render("exam_results.html", **_exam_ctx(
        title="Practice Exam", back_href="/", back_label="Home",
        result=result, pass_mark=26, total=35, pct=57, pass_pct=74, breakdown=[]))
    assert "exam-results--fail" in html
    assert "NOT YET" in html


# --- opted-in lesson decoder panels in a refusal --------------------------------
# A refusal's end (the cause, the remedy, the countdown) is what a learner acts on, and a long
# refusal runs past the two lines the fixed 124 px panel holds. In a lesson, panels that opt into
# .decoder--fits-refusal grow for a refusal up to a cap of lines and scroll past them. These read
# the stylesheet and templates; the render at 800x480 is what proves the layout.

DECODER_PANEL_JS = TEMPLATES_DIR.parent / "static" / "decoder-panel.js"
PANELS_HTML = TEMPLATES_DIR / "_panels.html"
FITS_REFUSAL = "decoder--fits-refusal"
REFUSAL_RULE_PREFIX = ".stage-panel--instrument .instrument > .decoder--fits-refusal:has("
REFUSAL_HIDE_SELECTOR = (
    ".decoder:has(> .decoder__head > :is(.decoder__status--empty, .decoder__status--stopped)) "
    "> :not(.decoder__head, .decoder__empty)"
)
OPTED_IN_PANEL_MACROS = frozenset({
    "flock", "decode", "rds", "aprs", "pocsag", "acars", "ais", "dmr", "apt", "sstv", "flipper",
    "survey",
})
OPTED_OUT_PANEL_MACROS = frozenset({"adsb", "lora", "rayhunter"})
# The lines of message the fixed panel shows when no rule lets it grow (measured at 800x480).
FIXED_PANEL_MESSAGE_LINES = 2
# The panel head's height at 800x480 (title, 13 px badge, padding, rule), measured in the render
# (41.84375 px in Chromium): not one CSS value, so it is held here and pinned to the badge size
# below. 124 px less the 6 px border, this head, the message's 20 px padding and two 26.1 px lines
# leaves 3.96 px a third line does not fit.
DECODER_HEAD_PX = 41.84
DECODER_BADGE_FONT_SIZE = "13px"
# The lowest the lesson's Prev and Next keys may sit while a four-line refusal grows the panel:
# 416.24 px by exact CSS arithmetic (416.22 px rendered in Chromium due to Blink 1/64 px line-height
# rounding on 18 * 1.45).
KEYS_BOTTOM_LIMIT_PX = 416.24
SCREEN_HEIGHT_PX = 480
CONSOLE_HTML = TEMPLATES_DIR / "console.html"


def _css_rules(css=None):
    """Every top-level (selector, body) in app.css whose selector is on one line."""
    css = APP_CSS.read_text() if css is None else css
    return re.findall(r"\n([^\n{}]+) \{([^{}]*)\}", css)


def _css_decl(selector, prop, css=None):
    """The value of `prop` in any top-level rule for exactly this selector, else None."""
    for sel, body in _css_rules(css):
        if sel == selector:
            m = re.search(r"(?:^|[;\s])" + re.escape(prop) + r":\s*([^;]+);", body)
            if m:
                return m.group(1).strip()
    return None


def _px(value):
    return float(re.fullmatch(r"([\d.]+)px", value.split()[0]).group(1))


def _refusal_rules(css=None):
    """The lesson Flock panel's refusal rules: (panel rule, message rule), each (selector, body)
    or None."""
    panel = message = None
    for sel, body in _css_rules(css):
        if FITS_REFUSAL not in sel:
            continue
        if sel.endswith("> .decoder__empty"):
            message = (sel, body)
        else:
            panel = (sel, body)
    return panel, message


def _rule_heads_naming(name, css=None):
    """(selector list, depth) for every rule in app.css whose selector names `name`, nested or
    not. Comments and strings are blanked first, then braces are counted; depth 0 is top level."""
    css = APP_CSS.read_text() if css is None else css
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    css = re.sub(r"\"[^\"\n]*\"|'[^'\n]*'", '""', css)
    found, depth, start = [], 0, 0
    for i, ch in enumerate(css):
        if ch == "{":
            head = " ".join(css[start:i].split())
            if name in head:
                found.append((head, depth))
            depth, start = depth + 1, i + 1
        elif ch in "};":
            depth, start = depth - (ch == "}"), i + 1
    return found


def _selectors(head):
    """A selector list split at its top-level commas (not those inside :is(...) or :has(...))."""
    parts, depth, cur = [], 0, ""
    for ch in head:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return parts + [cur.strip()]


def _decls(body):
    """A rule body's declarations as {property: value}, comments dropped. The last declaration
    counts with or without its semicolon (CSS needs none there, and Chrome applies it); a part
    with no colon is kept under its own text, so it can never pass as nothing."""
    decls = {}
    for part in re.sub(r"/\*.*?\*/", " ", body, flags=re.S).split(";"):
        prop, colon, value = part.partition(":")
        if part.strip():
            decls[prop.strip() if colon else part.strip()] = value.strip()
    return decls


def refusal_lines_shown(css=None):
    """How many lines of a refusal lesson 18's Flock panel shows: the cap in its refusal rule, no
    limit (inf) if the panel grows uncapped, or the fixed panel's lines if it does not grow."""
    panel, message = _refusal_rules(css)
    if panel is None or _decls(panel[1]).get("height") != "auto":
        return FIXED_PANEL_MESSAGE_LINES
    cap = message and re.fullmatch(r"calc\((\d+)lh \+ 20px\)",
                                   _decls(message[1]).get("max-height", ""))
    return int(cap.group(1)) if cap else math.inf


def test_only_a_refusal_grows_the_lesson_flock_panel():
    panel, message = _refusal_rules()
    assert panel is not None and message is not None, "no refusal rule for the lesson Flock panel"
    # Lessons only: EVERY rule that names the class, at any depth and in any selector list, is the
    # lesson's refusal rule, so the rack's panel (.rack__panel > .decoder) and explain stay out of
    # it whatever order the rules are in. A rule inside @media or another at-rule fails too.
    heads = _rule_heads_naming(FITS_REFUSAL)
    for head, depth in heads:
        assert depth == 0, f"nested in an at-rule: {head}"
        for sel in _selectors(head):
            assert sel.startswith(REFUSAL_RULE_PREFIX), sel
    assert len(heads) == 2, heads      # the panel rule and the message rule, nothing else
    # Exactly the badges a refusal gets (decoder-panel.js downState), never a live or starting one.
    js = DECODER_PANEL_JS.read_text()
    down = re.search(r"function downState\(.*?\n  \}\n", js, re.S).group(0)
    live = re.search(r"function liveBadge\(.*?\n  \}\n", js, re.S).group(0)
    refusal_badges = set(re.findall(r"decoder__status--[\w-]+", down))
    live_badges = set(re.findall(r"decoder__status(?:--[\w-]+)?", live))
    named = re.search(r":is\(([^)]*)\)", panel[0]).group(1)
    badges = {b.strip().lstrip(".") for b in named.split(",")}
    assert badges == refusal_badges, (badges, refusal_badges)
    assert not badges & live_badges, badges
    # At its size at rest for a short refusal, so only a long one changes anything.
    decls = _decls(panel[1])
    assert decls.get("height") == "auto", panel[1]
    assert decls.get("min-height") == _css_decl(".decoder", "height"), panel[1]
    # Exactly the approved macros opt in; adsb (radar), lora and rayhunter stay out.
    html = PANELS_HTML.read_text()
    macros = dict(re.findall(r"\{% macro (\w+)\([^)]*\) %\}(.*?)\{% endmacro %\}", html, re.S))
    opted_in = set()
    for name, body in macros.items():
        root = re.search(r"<div\b[^>]*>", body)
        if root and re.search(r'class="[^"]*\b' + re.escape(FITS_REFUSAL) + r'\b', root.group(0)):
            opted_in.add(name)
    assert opted_in == OPTED_IN_PANEL_MACROS
    assert not (opted_in & OPTED_OUT_PANEL_MACROS)
    assert OPTED_OUT_PANEL_MACROS <= set(macros)


def test_a_grown_refusal_keeps_the_lesson_keys_and_complete_on_screen():
    # Pin the badge size DECODER_HEAD_PX was measured at so a badge change cannot silently pass.
    assert _css_decl(".decoder__status", "font-size") == DECODER_BADGE_FONT_SIZE
    lines = refusal_lines_shown()
    assert lines != math.inf, "the refusal rule lets the panel grow without a cap"
    bar = _px(_css_decl(".app", "--bar-h"))
    rail = _px(_css_decl(".app", "--rail-h"))
    touch = _px(_css_decl(".app", "--touch"))
    pad = _px(_css_decl(".stage-panel", "padding"))
    title = _px(_css_decl(".stage-panel__title", "font-size")) * float(
        _css_decl(".stage-panel__title", "line-height"))
    row_gap = _px(_css_decl(".stage-panel--instrument", "row-gap"))
    gap = _px(_css_decl(".instrument", "gap"))
    panel_h = _px(_css_decl(".decoder", "height"))
    border = 2 * _px(_css_decl(".decoder", "border"))
    msg_pad = 2 * _px(_css_decl(".decoder__empty", "padding"))
    line_height = float(_css_decl("body", "line-height"))
    msg_line = _px(_css_decl(".decoder__empty", "font-size")) * line_height
    narr_line = _px(_css_decl(".stage-panel__body", "font-size")) * line_height
    panel_top = bar + rail + pad + title + row_gap                    # 158 px at 800x480
    band = SCREEN_HEIGHT_PX - pad - panel_top - panel_h - 2 * gap - touch   # the narration: 90
    spare = panel_h - border - DECODER_HEAD_PX - msg_pad - 2 * msg_line
    grown = panel_h if lines <= 2 else panel_h + (msg_line - spare) + (lines - 3) * msg_line
    keys_bottom = panel_top + grown + gap + touch
    assert round(keys_bottom, 2) <= KEYS_BOTTOM_LIMIT_PX, keys_bottom
    assert round(band - (grown - panel_h), 2) >= round(narr_line, 2), "no narration line left"
    # The figure holds only if the refusal rules add nothing but the growth and the cap: a padding
    # or margin there would move the keys without changing the line count above.
    panel, message = _refusal_rules()
    if panel is not None:
        assert _decls(panel[1]) == {"height": "auto",
                                    "min-height": _css_decl(".decoder", "height")}, panel[1]
    if message is not None:
        assert set(_decls(message[1])) == {"max-height", "overflow-y"}, message[1]


# --- lesson 39's Wi-Fi census in the fixed panel ----------------------------------------------
# M39's observe asks the learner to compare channels, so the census has to show more than one row
# at a time in the fixed 124 px panel. Measured at 800x480 before the fix: a 76 px list window and
# 61 px rows (a label line over a summary line), so one row and a sliver were in view.

def _survey_list_classes():
    """The classes on the survey macro's census list."""
    html = PANELS_HTML.read_text()
    body = re.search(r"\{% macro survey\([^)]*\) %\}(.*?)\{% endmacro %\}", html, re.S).group(1)
    ul = re.search(r"<ul\b[^>]*\bdata-survey-list\b[^>]*>", body).group(0)
    return re.search(r'class="([^"]*)"', ul).group(1).split()


def survey_census_rows_in_view(css=None):
    """Whole census rows the lesson's survey list shows at 800x480, by CSS arithmetic: the panel
    less its border and head, over one row's padding, grid lines, row gap and rule."""
    decls = {}
    for sel, body in _css_rules(css):
        if sel == ".decoder__item":
            decls.update(_decls(body))
    for cls in _survey_list_classes():
        for sel, body in _css_rules(css):
            if sel in (f".{cls} > .decoder__item", f".{cls} .decoder__item"):
                decls.update(_decls(body))
    lines = len(re.findall(r'"[^"]*"', decls["grid-template-areas"]))
    pad_y = _px(decls["padding"])
    row_gap = _px(decls.get("row-gap") or decls["gap"])
    line = _px(decls["font-size"]) * float(_css_decl("body", "line-height", css))
    rule = _px(decls["border-bottom"])
    row = 2 * pad_y + lines * line + (lines - 1) * row_gap + rule
    window = (_px(_css_decl(".decoder", "height", css))
              - 2 * _px(_css_decl(".decoder", "border", css)) - DECODER_HEAD_PX)
    return math.floor(window / row)


def test_the_lesson_survey_census_shows_at_least_two_whole_rows():
    # DECODER_HEAD_PX was measured with this badge size; a change to it must re-measure.
    assert _css_decl(".decoder__status", "font-size") == DECODER_BADGE_FONT_SIZE
    assert survey_census_rows_in_view() >= 2


def _census_decls(suffix, css=None):
    """Declarations of every top-level census-list rule whose selector ends with `suffix`."""
    decls = {}
    for sel, body in _css_rules(css):
        for part in _selectors(sel):
            if part.startswith(".decoder__list--census") and part.endswith(suffix):
                decls.update(_decls(body))
    return decls


def test_a_census_row_stays_one_line_whatever_the_ssid_lengths():
    # The row count above holds only while a row is one line. A busy 2.4 GHz channel's SSIDs can
    # run to 32 characters each; the column holds about 54, so a wrapped summary would show one
    # row again. Nothing in a row wraps: the SSID names shrink with an ellipsis, and the "+N",
    # hidden and open counts beside them never shrink. survey.js puts them in these two spans.
    row = _census_decls("> .decoder__item")
    assert row.get("white-space") == "nowrap", row
    read = _census_decls(".decoder__read")
    assert read.get("display") == "flex" and read.get("min-width") == "0", read
    names = _census_decls(".census__names")
    assert names.get("overflow") == "hidden" and names.get("text-overflow") == "ellipsis", names
    assert names.get("min-width") == "0", names
    tail = _census_decls(".census__tail")
    assert tail.get("flex") == "none" or tail.get("flex-shrink") == "0", tail


def test_radar_and_gallery_panels_match_the_refusal_hide_rule():
    # app.css hides every direct child of .decoder except head and empty during a refusal. That is
    # what collapses the gallery frame on apt/sstv and gives the radar its full height on adsb.
    assert _css_decl(REFUSAL_HIDE_SELECTOR, "display") == "none"
    html = PANELS_HTML.read_text()
    macros = dict(re.findall(r"\{% macro (\w+)\([^)]*\) %\}(.*?)\{% endmacro %\}", html, re.S))
    for name, expected_children in (
        ("adsb", ("radar__scope", "radar__count")),
        ("apt", ("gallery__frame",)),
        ("sstv", ("gallery__frame",)),
    ):
        body = macros[name]
        # The root element is a .decoder, and each target child sits at depth 1 inside it.
        root_match = re.search(r'<div class="([^"]*)"', body)
        assert root_match and "decoder" in root_match.group(1).split(), (name, body)
        inner = body[root_match.end():]
        depth = 0
        direct_classes = []
        for tag in re.finditer(r"<(/?)(div|figure|ul|aside|p|section)\b([^>]*)>", inner):
            closing, _elem, attrs = tag.groups()
            if closing:
                depth -= 1
                if depth < 0:
                    break
            else:
                if depth == 0:
                    cls = re.search(r'class="([^"]*)"', attrs)
                    if cls:
                        direct_classes.extend(cls.group(1).split())
                depth += 1
        for child_cls in expected_children:
            assert child_cls in direct_classes, (name, child_cls, direct_classes)
    # Explain holds Complete lesson (468 px, measured in the render, not computed here). What keeps
    # the rules off it is their .stage-panel--instrument prefix (test above) and that console.html
    # gives that class only to the stages in its INSTRUMENT list, which explain is not in.
    html = CONSOLE_HTML.read_text()
    instrument = re.search(r"\{% set INSTRUMENT = \[([^\]]*)\] %\}", html).group(1)
    assert "'explain'" not in instrument, instrument
    gives = re.findall(r"\{% if ([^%]*) %\} stage-panel--instrument\{% endif %\}", html)
    assert gives == ["stage in INSTRUMENT and lesson.needs_hardware"], gives
    assert html.count("stage-panel--instrument") == 1


def test_a_refusal_past_the_cap_scrolls_and_is_never_clipped():
    panel, message = _refusal_rules()
    assert panel is not None and message is not None, "no refusal rule for the lesson Flock panel"
    message_decls, panel_decls = _decls(message[1]), _decls(panel[1])
    assert re.fullmatch(r"calc\(\d+lh \+ 20px\)", message_decls.get("max-height", "")), message[1]
    # auto, not scroll: a scroll track on every refusal would look as if something scrolled.
    assert message_decls.get("overflow-y") == "auto", message[1]
    # The panel itself (overflow: hidden at rest) always holds the whole capped box.
    assert not {"max-height", "overflow", "overflow-y"} & set(panel_decls), panel[1]


# --- C24: the Restart sweep key ----------------------------------------------------------------
# The owner's ruling: a Restart sweep key on every live waterfall, shown only while it says
# STALLED (waterfall.js decides), and hidden until then. It rides the waterfall macro, so every
# lesson and rack view that draws a live waterfall carries one per waterfall, and nothing else does.

REAL_PACKS = TEMPLATES_DIR.parent.parent / "lesson_packs"
_OTHER_INSTRUMENTS = ("uses_decoder", "uses_lora", "uses_rds", "uses_adsb", "uses_aprs",
                      "uses_pocsag", "uses_acars", "uses_ais", "uses_dmr", "uses_apt", "uses_sstv",
                      "uses_flock", "uses_rayhunter", "uses_flipper", "uses_survey")
_RESTART_KEY = re.compile(r"<button[^>]*data-wf-restart[^>]*>([^<]*)</button>")


def _check_restart_keys(html, where):
    """One hidden Restart sweep key per live waterfall, between its badge and its reason slot."""
    wraps = re.findall(r'class="waterfall-wrap">(.*?)</div>\s*</div>', html, re.S)
    assert wraps, f"{where}: no live waterfall"
    keys = _RESTART_KEY.findall(html)
    assert len(keys) == len(wraps), f"{where}: {len(keys)} keys for {len(wraps)} waterfalls"
    for wrap in wraps:
        key = _RESTART_KEY.search(wrap)
        assert key, f"{where}: a waterfall without its key"
        assert key.group(1).strip() == "Restart sweep"
        assert " hidden" in key.group(0) and 'type="button"' in key.group(0)
        assert wrap.index("badge") < key.start() < wrap.index("data-wf-reason")


def test_every_live_waterfall_carries_one_hidden_restart_sweep_key():
    lessons = load_lessons(REAL_PACKS)
    waterfall_lessons = [lid for lid, lesson in lessons.items() if lesson.needs_hardware
                         and not any(getattr(lesson, flag) for flag in _OTHER_INSTRUMENTS)]
    assert sorted(waterfall_lessons, key=int) == [1, 2, 3, 4, 6, 7, 8, 9]
    for lid, lesson in lessons.items():
        committed = _console(lesson=lesson, prediction_committed=True, current_stage="explain")
        gate = _console(lesson=lesson, current_stage="predict")
        if lid in waterfall_lessons:
            _check_restart_keys(committed, f"lesson {lid}")
            _check_restart_keys(gate, f"lesson {lid} before the commit")
        else:
            assert "data-wf-restart" not in committed + gate, f"lesson {lid}"
    for key in ("waterfall", "clock", "health"):
        html = _render("operate.html", **_status(), instruments=INSTRUMENTS, inst=instrument(key),
                       freq_hz=98_000_000, flock_tool="tcpdump", text_withheld=False)
        _check_restart_keys(html, f"rack {key}")
    for inst in INSTRUMENTS:
        if inst.api != "spectrum":
            html = _render("operate.html", **_status(), instruments=INSTRUMENTS, inst=inst,
                           freq_hz=98_000_000, flock_tool="tcpdump", text_withheld=False)
            assert "data-wf-restart" not in html, f"rack {inst.key}"


def test_the_restart_key_stays_hidden_until_waterfall_js_shows_it():
    # The key is a house key (.btn), whose display: inline-flex beats the hidden attribute: without
    # its own [hidden] rule it would show on every waterfall, stalled or not.
    assert re.search(r"display:\s*none", _css_rule(".waterfall__restart[hidden]"))
    assert re.search(r"position:\s*absolute", _css_rule(".waterfall__restart"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


@pytest.mark.parametrize("stage", ["explain", "check"])
def test_completion_has_a_body_size_status_beside_save_and_keeps_back(stage):
    lesson = _intro_lesson() if stage == "explain" else load_lessons(REAL_PACKS)[1]
    html = _console(
        lesson=lesson, current_stage=stage, prediction_committed=True,
        check_feedback={"correct": True, "chosen": {"label": "A", "why": "It fits."}},
    )
    panel = _section(html, stage)
    action_class = "stage-foot" if stage == "explain" else "predict__actions"
    row = re.search(r'class="' + action_class + r'\b[^"]*">(.*?)(?:</footer>|</div>)',
                    panel, re.S).group(1)
    status = re.search(r"<span\b([^>]*)>\s*</span>", row)
    assert status and 'role="status"' in status[1] and "data-complete-status" in status[1]
    assert 'class="predict__hint"' in status[1]
    assert _css_decl(".predict__hint", "font-size") == "18px"
    assert re.search(r"<button[^>]*\bdata-complete\s[^>]*>Complete lesson</button>", row)
    assert 'class="btn-back"' in html


@pytest.mark.parametrize("template", ["exam.html", "exam_sim.html"])
def test_exam_eyebrow_gets_its_emblem_only_from_css(template):
    q = exam.load_pool().get("T1A01")
    html = _render(template, **_exam_ctx(
        question=q, reviewed=False, position=1, total=35, score=0, answered=0, pass_mark=26,
    ))
    eyebrow = re.search(r'class="stage-panel__eyebrow">([^<]+)</div>', html)[1]
    assert eyebrow.startswith("EXAM" if template == "exam.html" else "PRACTICE EXAM")
    assert "\u262d" not in eyebrow and "&#9773;" not in eyebrow
    assert _css_decl(".stage-panel__eyebrow::before", "content") == r'"\262D "'


def test_active_tile_cursor_stays_with_the_left_aligned_code():
    assert _css_decl(".tile--active .tile__head::before", "content") is None
    marker = ".tile--active .tile__code::before"
    assert _css_decl(marker, "content") == r'"\25B6"'
    assert _css_decl(marker, "animation").startswith("blink ")


def test_unit_grid_has_no_three_pixel_padding_overflow_without_shrinking_tiles():
    # Unit 4 originally measured 417px in a 414px region with 14px insets and a 66px bar.
    # Take the taller bar out of those insets so the tiles themselves keep their size.
    initial_inset = _px(_css_decl(".tiles", "padding"))
    top = _px(_css_decl(".content > .tiles:last-child", "padding-top"))
    bottom = _px(_css_decl(".content > .tiles:last-child", "padding-bottom"))
    view = 480 - _px(_css_decl(".app", "--bar-h"))
    assert 417 - 2 * initial_inset + top + bottom <= view
    assert _css_decl(".tiles", "overflow-y") == "auto"
    assert _css_decl(".tiles", "grid-auto-rows") == "max-content"
    assert _decls(_css_rule(".tile"))["min-height"] == "124px"


@pytest.mark.parametrize("form", ["predict", "check", "exam.html", "exam_sim.html"])
def test_choice_commit_has_a_themed_gate_without_native_popup(form):
    if form in ("predict", "check"):
        lesson = load_lessons(REAL_PACKS)[1]
        whole = _console(lesson=lesson, current_stage=form, prediction_committed=form == "check")
        html = _section(whole, form)
    else:
        q = exam.load_pool().get("T1A01")
        whole = html = _render(form, **_exam_ctx(question=q, reviewed=False, can_skip=True))
    assert '/static/lesson-nav.js' in whole
    assert re.search(r"<form[^>]*\bnovalidate\b[^>]*>", html)
    assert re.search(r"<form[^>]*\bdata-choice-commit\b[^>]*>", html)
    assert re.search(r'<input type="radio"[^>]*\brequired\b[^>]*>', html)
    assert re.search(r'<button[^>]*\bbtn-commit\b[^>]*\bdisabled\b[^>]*>', html)
    assert re.search(r'<span[^>]*role="status"[^>]*>Choose one, comrade</span>', html)


def test_receiver_macros_start_checking_and_adsb_count_is_hidden():
    html = _console(lesson=_intro_lesson(), current_stage="demo")
    assert "Checking receiver…" in html
    assert "SAMPLE &middot; NOT LIVE" not in html
    rack = _render("operate.html", **_status(), inst=instrument("adsb"),
                   instruments=INSTRUMENTS)
    assert "Checking receiver…" in rack
    assert re.search(r'<div class="radar__count" hidden>', rack)


def test_shared_refusal_badge_reads_on_every_theme_including_the_waterfall():
    selector = ".badge--na, .decoder__status--empty, .decoder__status--stopped"
    rule = _decls(_css_rule(selector))
    ink = re.fullmatch(r"var\((--[a-z-]+)\)", rule["color"])[1]
    paper = re.fullmatch(r"var\((--[a-z-]+)\)", rule["background"])[1]
    for theme in THEMES:
        tokens = _theme_tokens(theme)
        assert _contrast(tokens[ink], tokens[paper]) >= 4.5, theme


def test_status_bar_controls_get_the_full_touch_height_inside_the_bar():
    bar = _css_rule(".statusbar")
    assert _decls(bar)["align-items"] == "stretch"
    assert _px(_css_decl(".app", "--bar-h")) - _px(_decls(bar)["border-bottom"]) >= 72
    for control in (".btn-back", ".btn-theme", ".btn-exit"):
        assert not re.search(rf"{control}::(?:after|before)\s*\{{", APP_CSS.read_text())
    header = _render("base.html", **_status(exit_to_desktop=True))
    assert re.search(r'<button class="btn-back btn-exit" type="button"', header)
    assert re.search(r'data-exit-armed hidden>Tap again<br>to exit<', header)


@pytest.mark.parametrize("stage", ["predict", "check"])
def test_lesson_gates_recover_the_header_height_without_shrinking_touch_targets(stage):
    header_and_rail = sum(_px(_css_decl(".app", token)) for token in ("--bar-h", "--rail-h"))
    stage_rule = _decls(_css_rule(f'.stage-panel[data-stage="{stage}"]'))
    assert header_and_rail > 66 + 40
    assert header_and_rail + _px(stage_rule["padding-top"]) + _px(stage_rule["padding-bottom"]) <= (
        66 + 40 + 2 * 6
    )
    assert _px(_css_decl(".app", "--touch")) >= 72


def test_rack_picker_selection_uses_the_filled_home_tile_palette():
    for prop in ("background", "border-color", "color"):
        assert _css_decl(".rack__pick--on", prop) == _css_decl(".tile--active", prop)
    assert _css_decl(".rack__pick--on .rack__pick-tool", "color") == "inherit"


def test_scroll_cue_frames_wrap_home_lesson_text_and_operator_picker():
    home = _render("home.html", **_status(), tiles=UNITS)
    home = re.sub(r"<!--.*?-->", "", home, flags=re.S)
    assert re.search(r'class="scroll-frame home-grid">\s*<[^>]*class="tiles"', home)
    lesson = _console(lesson=_intro_lesson(), current_stage="demo")
    for stage in ("frame", "demo"):
        assert re.search(r'class="scroll-frame">\s*<div class="stage-panel__body">\s*'
                         r'<p class="scroll-copy">', _section(lesson, stage))
    rack = _render("operate.html", **_status(), inst=instrument("adsb"), instruments=INSTRUMENTS)
    assert re.search(r'class="scroll-frame rack__chooser">\s*<nav class="rack__picker"', rack)
    assert _px(_css_decl(".home-grid .tile", "min-height")) < _px(
        _decls(_css_rule(".tile"))["min-height"])


def test_adsb_labels_live_outside_the_scaled_svg_with_a_css_pixel_floor():
    html = _render("operate.html", **_status(), inst=instrument("adsb"), instruments=INSTRUMENTS)
    plot = re.search(r'<div class="radar__plot">\s*(<svg.*?</svg>)(.*?)</div>', html, re.S)
    assert plot
    assert "<text " not in plot[1]
    assert all(f">{point}<" in plot[2] for point in "NESW")
    assert "data-adsb-labels" in html
    for label in (".radar__compass", ".radar__plane-label", ".radar__ring-label"):
        assert _px(_css_decl(label, "font-size")) >= 13


def test_refusals_centre_the_message_and_keep_a_command_whole():
    condition = (".decoder:has(> .decoder__head > "
                 ":is(.decoder__status--empty, .decoder__status--stopped))")
    assert _css_decl(condition + " > :not(.decoder__head, .decoder__empty)", "display") == "none"
    assert _css_decl(condition + " > .decoder__empty", "margin-block") == "auto"
    assert _css_decl(condition + " > .decoder__empty", "overflow-y") == "auto"
    assert _css_decl(".refusal-command", "white-space") == "nowrap"


def test_lesson_instrument_captions_leave_activity_to_the_status_badge():
    caption_pattern = re.compile(r'<div class="tune__caption"[^>]*>(.*?)</div>', re.S)
    activity = re.compile(r"\b(?:decoding|listening|sweeping)\b", re.I)
    claims = []
    for lid, lesson in load_lessons(REAL_PACKS).items():
        if not lesson.needs_hardware:
            continue
        html = _console(lesson=lesson, prediction_committed=True, current_stage="explain")
        captions = caption_pattern.findall(html)
        assert captions, f"lesson {lid} has an instrument but no dial caption"
        for caption in sorted(set(captions)):
            if activity.search(re.sub(r"<[^>]*>", "", caption)):
                claims.append(f"lesson {lid}: {caption}")
    assert not claims, "\n".join(claims)


@pytest.mark.parametrize("key", ["waterfall", "aprs"])
def test_operator_adjust_keys_name_the_quantity_on_the_key(key):
    html = _render("operate.html", **_status(), inst=instrument(key), instruments=INSTRUMENTS,
                   freq_hz=144_390_000)
    pairs = {"tune": "Freq", "gain": "Gain"} if key == "waterfall" else {"tune": "Freq"}
    for control, label in pairs.items():
        for direction in (-1, 1):
            hook = f'data-{control}-step="{direction}"'
            button = re.search(rf'<button[^>]*{hook}[^>]*>(.*?)</button>', html, re.S)
            assert button and f">{label}<" in button[1]


@pytest.mark.parametrize("template", ["exam.html", "exam_sim.html"])
@pytest.mark.parametrize("reviewed", [False, True])
def test_exam_body_has_the_more_below_cue_in_study_and_practice(template, reviewed):
    html = _render(template, **_exam_ctx(
        question=exam.load_pool().get("T0A01"), reviewed=reviewed,
        position=1, total=35, score=0, answered=0, pass_mark=26))
    assert re.search(r'<div class="scroll-frame">\s*<div class="exam__body">', html)
    assert 'src="/static/scroll-keys.js"' in html


@pytest.mark.parametrize(("lesson_id", "instrument_key", "control"), [
    (3, "health", "data-prov-capture"),
    (4, "clock", "data-ppm-measure"),
])
def test_lesson_next_is_primary_beside_a_secondary_instrument_action(
    lesson_id, instrument_key, control,
):
    lesson = load_lessons(REAL_PACKS)[lesson_id]
    demo = _section(_console(lesson=lesson, current_stage="demo"), "demo")
    keys = re.findall(r'<button[^>]*class="[^"]*btn--primary[^"]*"[^>]*>', demo)
    assert len(keys) == 1 and 'data-nav="next"' in keys[0]
    assert re.search(rf'<button class="[^"]*btn--secondary[^"]*"[^>]*{control}', demo)
    rack = _render("operate.html", **_status(), inst=instrument(instrument_key),
                   instruments=INSTRUMENTS, freq_hz=98_100_000)
    assert re.search(rf'<button class="[^"]*btn--primary[^"]*"[^>]*{control}', rack)
