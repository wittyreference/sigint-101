# ABOUTME: Check the rack LOG target and logbook layout at 800x480 over Chromium DevTools.
# ABOUTME: Verify Back, list scrolling, and the badge/replay distinction in all three themes.

import json
import sys
import time

from render_scroll import connect

THEMES = ("dark", "sunlight", "red-night")

RACK = """
(() => {
  const key = document.querySelector('.rack__log-link[href="/logbook"]');
  const title = document.querySelector('.statusbar__title');
  const rect = key?.getBoundingClientRect();
  if (!rect || !title) return null;
  return {target: [rect.width, rect.height], visible:
    rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight &&
    title.scrollWidth <= title.clientWidth};
})()
"""

LOGBOOK = """
function measureLogbook(expectedTheme) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back[href="/operate"]');
  const list = document.querySelector('.logbook__entries');
  const last = list?.querySelector('.logbook__entry:last-child');
  const stamp = document.querySelector('.logbook-mark--stamp');
  const replay = document.querySelector('.logbook-mark--replay');
  if (!app || !back || !last || !stamp || !replay) return null;

  function inViewport(element) {
    const r = element.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth && r.bottom <= innerHeight;
  }
  function luminance(rgb) {
    const channels = rgb.match(/[\\d.]+/g).slice(0, 3).map(Number).map(v => {
      const s = v / 255;
      return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  }
  function contrast(ink, paper) {
    const levels = [luminance(ink), luminance(paper)].sort((a, b) => b - a);
    return (levels[0] + 0.05) / (levels[1] + 0.05);
  }

  const stampStyle = getComputedStyle(stamp);
  const replayStyle = getComputedStyle(replay);
  const scrolls = getComputedStyle(list).overflowY === 'auto' &&
                  list.scrollHeight > list.clientHeight && list.scrollWidth <= list.clientWidth;
  const backBefore = inViewport(back);
  list.scrollTop = list.scrollHeight;
  const end = last.getBoundingClientRect().bottom;
  return {
    theme: app.dataset.theme,
    back: backBefore && inViewport(back) && back.getBoundingClientRect().height >= 72,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    scrolls,
    lastVisible: list.scrollTop > 0 && end <= list.getBoundingClientRect().bottom + 1 &&
                 inViewport(stamp) && inViewport(replay),
    contrast: [contrast(stampStyle.color, stampStyle.backgroundColor),
               contrast(replayStyle.color, getComputedStyle(app).backgroundColor)],
    differentMarks: stamp.textContent === 'STAMP' && replay.textContent === 'REPLAY' &&
                    stampStyle.borderRadius !== replayStyle.borderRadius &&
                    stampStyle.backgroundColor !== 'rgba(0, 0, 0, 0)' &&
                    replayStyle.backgroundColor === 'rgba(0, 0, 0, 0)' &&
                    replayStyle.borderTopStyle === 'solid',
  };
}
"""


def load_page(browser, url):
    nav = browser.call("Page.navigate", {"url": url})
    if nav.get("errorText"):
        raise RuntimeError(nav["errorText"])
    for _ in range(25):
        if browser.ev("document.readyState") == "complete":
            browser.call("Runtime.evaluate", {"expression": "document.fonts.ready",
                                              "awaitPromise": True})
            return
        time.sleep(0.2)
    raise RuntimeError("page did not finish loading")


def verdict(ok, label, measures):
    print(label, json.dumps(measures))
    print(f"VERDICT: {'PASS' if ok else 'FAIL'} - {label}")
    return not ok


def main(console):
    browser = connect()
    try:
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        load_page(browser, f"{console}/operate/ism")
        rack = browser.ev(RACK) or {}
        target_fits = rack.get("visible") and all(72 <= size <= 80 for size in rack["target"])
        failed = verdict(target_fits, "rack LOG key", rack)

        url = f"{console}/logbook"
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            load_page(browser, url)
            measures = browser.ev(f"{LOGBOOK}\nmeasureLogbook({json.dumps(theme)})") or {}
            ok = (measures.get("theme") == theme and measures.get("doc") == [800, 480]
                  and measures["back"] and measures["scrolls"] and measures["lastVisible"]
                  and min(measures["contrast"]) >= 4.5 and measures["differentMarks"])
            failed |= verdict(ok, f"logbook {theme}", measures)
        return int(failed)
    finally:
        browser.s.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
