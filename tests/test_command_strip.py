# ABOUTME: Tests the console markup for the command strip (R12) on every process-backed panel.
# ABOUTME: The strip starts empty and hidden; only the live route's `command` ever fills it.

import re

import pytest

from sigint101.lessons import parse_lesson
from tests.test_templates import _console

# Panel name -> the data attribute its panel carries. Each runs a real process the strip names.
PANELS = {
    "decode": "data-decode-panel", "rds": "data-rds-panel", "adsb": "data-adsb-panel",
    "aprs": "data-aprs-panel", "pocsag": "data-pocsag-panel", "acars": "data-acars-panel",
    "ais": "data-ais-panel", "dmr": "data-dmr-panel", "apt": "data-apt-panel",
    "sstv": "data-sstv-panel", "flock": "data-flock-panel",
}


def _lesson(panel):
    return parse_lesson({
        "id": 40, "slug": panel, "title": "t", "type": "intro", "concept": "c",
        "panels": [panel], "hardware": ["rtl-sdr"],
        "stages": {"frame": "f", "demo": "DEMO", "try": "t", "predict": "p", "observe": "o",
                   "explain": "e"},
    })


def _panel_markup(html, attr):
    """The markup of the one decoder panel carrying `attr`, up to the tune strip after it."""
    start = html.index(attr)
    return html[start:html.index('class="tune', start)]


@pytest.mark.parametrize("panel", sorted(PANELS))
def test_every_process_panel_carries_an_empty_hidden_command_strip(panel):
    html = _console(lesson=_lesson(panel), current_stage="demo", tuned_hz=144_390_000)
    markup = _panel_markup(html, PANELS[panel])
    strip = re.search(r"<button[^>]*data-command[^>]*>.*?</button>", markup, re.S)
    assert strip, f"no command strip in the {panel} panel"
    tag = strip.group(0)
    assert " hidden" in tag.split(">", 1)[0]          # nothing runs until the route says so
    # Never a hardcoded command: the text element is empty until the live route fills it.
    assert re.search(r"<code[^>]*data-command-text[^>]*></code>", tag)


def test_the_strip_helper_loads_before_the_panels_that_use_it():
    html = _console(lesson=_lesson("aprs"), current_stage="demo", tuned_hz=144_390_000)
    helper = html.index('src="/static/decoder-panel.js"')
    for panel in PANELS:
        assert helper < html.index(f'src="/static/{panel}.js"')


def test_the_waterfall_instrument_leaves_a_command_hook():
    # The waterfall owner wires it from /api/spectrum's `command`; it starts empty and hidden.
    html = _console(lesson=parse_lesson({
        "id": 41, "slug": "wf", "title": "t", "type": "intro", "concept": "c",
        "panels": [], "hardware": ["rtl-sdr"],
        "stages": {"frame": "f", "demo": "DEMO", "try": "t", "predict": "p", "observe": "o",
                   "explain": "e"},
    }), current_stage="demo")
    assert re.search(r"<button[^>]*data-command[^>]*hidden", html)
