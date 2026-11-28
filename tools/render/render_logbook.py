# ABOUTME: Check the rack LOG target and logbook layout at 800x480 over Chromium DevTools.
# ABOUTME: Verify Back, list scrolling, and the badge/replay distinction in all three themes.

import json
import sys
import time

from render_scroll import connect, fence, leave_page

RENDER_CHECK = {
    "name": "logbook",
    "pages": ("/operate/ism", "/logbook"),
    "scratch": False,
    "order": 40,
}

THEMES = ("dark", "sunlight", "red-night")

RACK = """
function measureRack(expectedTheme) {
  const key = document.querySelector('.rack__log-link[href="/logbook"]');
  const strip = document.querySelector('.rack__log[href="/logbook"][data-rack-log]');
  const title = document.querySelector('.statusbar__title');
  const rect = key?.getBoundingClientRect();
  if (!rect || !title) return null;
  function inViewport(r) {
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth && r.bottom <= innerHeight;
  }
  const log = strip?.getBoundingClientRect();
  const hit = log && document.elementFromPoint(log.left + log.width / 2, log.top + log.height / 2);
  return {
    theme: document.querySelector('.app').dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    target: [rect.width, rect.height],
    visible: inViewport(rect) && title.scrollWidth <= title.clientWidth,
    stripVisible: !!log && inViewport(log) && strip.contains(hit),
  };
}
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

  const meteorRow = list?.querySelector('[data-logbook-instrument="meteor"]');
  const aptRow = list?.querySelector('[data-logbook-instrument="apt"]');
  let meteorBadge = false;
  if (meteorRow && aptRow) {
    const meteorLabel = meteorRow.querySelector('strong')?.textContent?.trim();
    const aptLabel = aptRow.querySelector('strong')?.textContent?.trim();
    const saved = meteorRow.innerHTML;
    meteorRow.innerHTML =
      '<div class="logbook__heading"><strong>Meteor</strong>' +
      '<span class="logbook-mark logbook-mark--stamp">STAMP</span></div>' +
      '<p><time datetime="2026-11-28T12:00:00+00:00">2026-11-28 12:00:00 UTC</time>' +
      ' · satdump · 137.9 MHz</p>' +
      '<p>RTL-SDR rx01</p>' +
      '<p><code>satdump live meteor_m2-x_lrpt /var/lib/sigint101/data/meteor/work/pass ' +
      '--source rtlsdr --frequency 137900000 --finish_processing</code></p>';
    const rowPx = parseFloat(getComputedStyle(meteorRow).fontSize);
    meteorBadge = meteorLabel === 'Meteor' && aptLabel === 'APT' &&
                  rowPx >= 18 && meteorRow.scrollWidth <= meteorRow.clientWidth + 1;
    meteorRow.innerHTML = saved;
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
    meteorBadge,
  };
}
"""


def load_page(browser, url):
    # Through the render proxy, never the console itself (render_scroll.fence).
    nav = browser.call("Page.navigate", {"url": fence(url)})
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
        rack_url = f"{console}/operate/ism"
        load_page(browser, rack_url)
        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            load_page(browser, rack_url)
            rack = browser.ev(f"{RACK}\nmeasureRack({json.dumps(theme)})") or {}
            target_fits = (rack.get("theme") == theme and rack.get("doc") == [800, 480]
                           and rack["visible"] and rack["stripVisible"]
                           and all(72 <= size <= 80 for size in rack["target"]))
            failed |= verdict(target_fits, f"rack {theme}", rack)

            load_page(browser, f"{console}/logbook")
            measures = browser.ev(f"{LOGBOOK}\nmeasureLogbook({json.dumps(theme)})") or {}
            ok = (measures.get("theme") == theme and measures.get("doc") == [800, 480]
                  and measures["back"] and measures["scrolls"] and measures["lastVisible"]
                  and min(measures["contrast"]) >= 4.5 and measures["differentMarks"]
                  and measures.get("meteorBadge") is True)
            failed |= verdict(ok, f"logbook {theme}", measures)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
