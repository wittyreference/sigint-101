# ABOUTME: Renders the touch-console Jinja templates (base/console/home) with sample
# ABOUTME: contexts and asserts the shell, the keyboard-free predict gate, and the seal.

import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sigint101 import exam
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


def _eyebrow_for(html, stage):
    """The eyebrow text of one stage's panel (the first eyebrow after its section opens)."""
    m = re.search(
        r'data-stage="' + re.escape(stage) + r'".*?stage-panel__eyebrow">(.*?)</div>',
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


def test_instrument_stage_eyebrow_drops_the_repeated_concept():
    # The instrument stages (demo/try/observe) have a tight scrolling narration band at 800x480,
    # so their eyebrow carries only the step + stage label; the concept already rode the frame
    # stage and must not eat that band again here.
    lesson = _intro_lesson()
    html = _console(lesson=lesson, current_stage="demo", prediction_committed=False)
    demo_eyebrow = _eyebrow_for(html, "demo")
    assert "DEMO" in demo_eyebrow
    assert lesson.concept not in demo_eyebrow
    # The frame still carries the lesson's thesis.
    assert lesson.concept in _eyebrow_for(html, "frame")


def test_the_predict_eyebrow_drops_the_repeated_concept():
    # Predict is the tightest screen (prompt + frozen frame + chips + Commit all on 800x480), so its
    # eyebrow carries only the step + stage label; the concept already rode the frame stage.
    lesson = _intro_lesson()
    html = _console(lesson=lesson, current_stage="predict", prediction_committed=False)
    predict_eyebrow = _eyebrow_for(html, "predict")
    assert "PREDICT" in predict_eyebrow
    assert lesson.concept not in predict_eyebrow
    assert lesson.concept in _eyebrow_for(html, "frame")


def test_a_no_hardware_instrument_stage_keeps_the_concept_in_its_eyebrow():
    # A no-radio lesson's demo renders as plain text (no tight waterfall band), so it is not an
    # instrument stage and keeps the concept in its eyebrow like any text stage.
    lesson = _rules_lesson()
    html = _console(lesson=lesson, current_stage="demo")
    assert lesson.concept in _eyebrow_for(html, "demo")


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
        "tune": {"center_hz": 929_600_000, "span_hz": 200_000},
        "stages": {"frame": "f", "demo": "DEMO watch the pages arrive", "try": "t",
                   "predict": "p", "observe": "o", "explain": "e"},
    })


def test_a_pocsag_lesson_shows_the_page_log_not_a_waterfall():
    html = _console(lesson=_pocsag_intro_lesson(), current_stage="demo", tuned_hz=929_600_000)
    assert "data-pocsag-panel" in html
    assert "data-pocsag-list" in html
    assert "multimon-ng" in html
    assert "929.6" in html
    assert "canvas class=\"waterfall\"" not in html
    assert "data-aprs-panel" not in html


def test_a_pocsag_lessons_predict_frame_seals_the_log():
    html = _console(lesson=_pocsag_intro_lesson(), current_stage="predict",
                    prediction_committed=False, tuned_hz=929_600_000)
    assert "PAGES HIDDEN" in html
    assert "waterfall-wrap--frozen" not in html


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


def test_every_screen_has_no_horizontal_overflow_affordance():
    # Guard against a stray inline width that would break the 800x480 no-scroll rule;
    # this is a smoke check that the templates render without raising.
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
