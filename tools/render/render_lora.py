# ABOUTME: Checks the LoRa rack's Send strip and antenna notice at 800x480.
# ABOUTME: Measures the longest outcome in each theme with LoRa network requests blocked.

import json
import sys

from render_logbook import THEMES, load_page, verdict
from render_scroll import connect

REFUSAL = (
    "no LoRa radio — meshtasticd is not reachable at localhost:4403 "
    "([Errno 111] Connection refused)"
)

MEASURE = """
function measureLoRa(expectedTheme, refusal) {
  const app = document.querySelector('.app');
  const strip = document.querySelector('[data-lora-send-permit]');
  const button = strip?.querySelector('[data-lora-send]');
  const readout = strip?.querySelector('.rack-lora__readout');
  const outcome = strip?.querySelector('[data-lora-outcome]');
  const notice = strip?.querySelector('.safety-notice');
  const panel = document.querySelector('.rack__panel');
  const back = document.querySelector('.btn-back');
  if (!app || !button || !readout || !outcome || !notice || !panel || !back) return null;

  function visible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
      r.right <= innerWidth && r.bottom <= innerHeight &&
      el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1;
  }
  outcome.hidden = false;
  const shapes = ["handed", "repeated", "no-repeat"].map(state => {
    outcome.dataset.state = state;
    return getComputedStyle(outcome).borderLeftStyle;
  });
  readout.querySelector('[data-lora-radio]').textContent =
    'US · LONG_TURBO · frequency not reported · radio default';
  outcome.textContent = 'Previous send · No node repeated it. A mesh of one hears nothing.';
  const note = strip.querySelector('[data-lora-no-repeat]');
  note.hidden = false;
  function stripFits() {
    const r = button.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return [button, outcome, readout, notice, panel, back].every(visible) &&
      button.contains(hit) && document.querySelectorAll('.safety-notice').length === 1;
  }
  const fits = stripFits();
  readout.querySelector('[data-lora-radio]').textContent = 'Radio settings not reported';
  note.hidden = true;
  outcome.dataset.state = 'error';
  outcome.textContent = refusal;
  const r = button.getBoundingClientRect();
  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    send: [r.width, r.height],
    visible: fits,
    refusalVisible: stripFits(),
    shapes: new Set(shapes).size === 3,
  };
}
"""


def main(console):
    browser = connect()
    try:
        browser.call("Page.enable")
        browser.call("Network.enable")
        browser.call("Network.setBlockedURLs", {"urls": ["*/api/lora*"]})
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        url = f"{console}/operate/lora"
        load_page(browser, url)
        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            load_page(browser, url)
            measures = browser.ev(
                f"{MEASURE}\nmeasureLoRa({json.dumps(theme)}, {json.dumps(REFUSAL)})"
            ) or {}
            size = measures.get("send", [])
            ok = (measures.get("theme") == theme and measures.get("doc") == [800, 480]
                  and measures.get("visible") and measures.get("refusalVisible")
                  and measures.get("shapes")
                  and len(size) == 2 and all(72 <= dimension <= 80 for dimension in size))
            failed |= verdict(ok, f"LoRa send {theme}", measures)
        return int(failed)
    finally:
        browser.s.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
