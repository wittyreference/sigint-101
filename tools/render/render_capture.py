# ABOUTME: Checks Capture and its Waterfall key at 800x480 in the three kiosk themes.
# ABOUTME: Feeds fixture status to the browser while the shared proxy fences the console.

import json
import sys

from render_logbook import THEMES, load_page, verdict
from render_scroll import connect, leave_page

RENDER_CHECK = {
    "name": "capture",
    "pages": ("/capture", "/operate/waterfall"),
    "scratch": False,
    "order": 131,
}

STUB = r"""
(() => {
  const original = window.fetch;
  window.fetch = (address, options) => {
    if (new URL(address, location.href).pathname.startsWith('/api/capture')) {
      return Promise.resolve(new Response(JSON.stringify({
        state: sessionStorage.getItem('capture-fixture-state'),
        confirmed: {hz: 433921017, gain: '19.7'},
        tune: {hz: 433921017, gain: '19.7'},
        last_saved: sessionStorage.getItem('capture-fixture-state') === 'saved' ? {} : null,
        reason: sessionStorage.getItem('capture-fixture-reason'),
      }), {headers: {'content-type': 'application/json'}}));
    }
    return original(address, options);
  };
})()
"""

MEASURE = r"""
(() => {
  const within = (el) => {
    const r = el?.getBoundingClientRect();
    return !!r && r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth && r.bottom <= innerHeight &&
           el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1;
  };
  const pick = (selector) => document.querySelector(selector);
  const control = selector => {
    const el = pick(selector);
    const height = el?.getBoundingClientRect().height || 0;
    return within(el) && height >= 72 && height <= 80;
  };
  const law = pick('.capture__law');
  const bar = pick('.statusbar');
  const barRect = bar.getBoundingClientRect();
  const fitsBar = el => {
    if (!within(el)) return false;
    const box = el.getBoundingClientRect();
    return box.left >= barRect.left && box.top >= barRect.top &&
      box.right <= barRect.right && box.bottom <= barRect.bottom;
  };
  const siblingsFit = parent => {
    const shown = [...parent.children].filter(el => el.getClientRects().length);
    if (!shown.every(fitsBar)) return false;
    const boxes = shown.map(el => el.getBoundingClientRect());
    return boxes.every((a, i) => boxes.slice(i + 1).every(b =>
      a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top));
  };
  const status = pick('[data-capture-state]');
  return {
    theme: pick('.app').dataset.theme,
    width: document.documentElement.scrollWidth,
    height: document.documentElement.scrollHeight,
    back: control('.btn-back'),
    capture: law ? {
      record: control('[data-capture-start]'),
      stop: control('[data-capture-stop]'),
      law: within(law) && parseFloat(getComputedStyle(law).fontSize) >= 18 &&
        law.textContent.includes('2511(1)(a)') && law.textContent.includes('605(a)') &&
        law.getAttribute('href').startsWith('/law'),
      state: status.textContent,
      status: within(status),
      discard: pick('[data-capture-stop]').textContent === 'Stop and discard',
    } : null,
    entry: control('a.capture-link'),
    header: barRect.width === 800 && barRect.height === 75 && within(bar) &&
      siblingsFit(bar) && siblingsFit(pick('.statusbar__status')) &&
      siblingsFit(pick('.statusbar__readouts')) &&
      ['.btn-back', 'a.capture-link', 'a[href="/logbook"]', 'a[href="/field"]', '.btn-theme']
        .every(selector => control('.statusbar ' + selector) &&
          fitsBar(pick('.statusbar ' + selector))),
  };
})()
"""


def main(console):
    browser = connect()
    try:
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride", {
            "width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False,
        })
        browser.call("Page.addScriptToEvaluateOnNewDocument", {"source": STUB})
        load_page(browser, f"{console}/operate/waterfall")
        failures = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            load_page(browser, f"{console}/operate/waterfall")
            rack = browser.ev(MEASURE)
            screens = {}
            for state, expected, reason in (
                ("idle", "Ready to record", ""),
                ("recording", "Recording…", ""),
                ("saving", "Saving…", ""),
                ("saved", "Capture saved", ""),
                ("error", "no RTL-SDR on the USB bus", "no RTL-SDR on the USB bus"),
                ("error", "Capture storage is full: 25 MB capture quota",
                 "Capture storage is full: 25 MB capture quota"),
                ("error", "Capture storage is full: keep 64 MiB free",
                 "Capture storage is full: keep 64 MiB free"),
            ):
                browser.ev(f"sessionStorage.setItem('capture-fixture-state', {json.dumps(state)})")
                browser.ev(
                    f"sessionStorage.setItem('capture-fixture-reason', {json.dumps(reason)})")
                load_page(browser, f"{console}/capture")
                measure = browser.ev(MEASURE)
                for _ in range(5):
                    if measure["capture"]["state"] == expected:
                        break
                    browser.call("Runtime.evaluate", {
                        "expression": "new Promise(resolve => setTimeout(resolve, 200))",
                        "awaitPromise": True,
                    })
                    measure = browser.ev(MEASURE)
                screens[expected] = measure
            ok = (rack["theme"] == theme and rack["width"] == 800 and rack["height"] == 480
                  and rack["back"] and rack["entry"] and rack["header"])
            for expected, measure in screens.items():
                capture = measure["capture"]
                ok &= (measure["theme"] == theme and measure["width"] == 800
                       and measure["height"] == 480 and measure["back"]
                       and all(capture[key] for key in ("record", "stop", "law", "status"))
                       and capture["state"] == expected
                       and capture["discard"] == (expected == "Saving…"))
            failures |= verdict(ok, f"capture {theme}", {"rack": rack, "capture": screens})
        return int(failures)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
