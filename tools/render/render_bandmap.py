# ABOUTME: Check Home's BANDS key and the /bands layout at 800x480 over Chromium DevTools.
# ABOUTME: Verifies touch targets, non-overlapping rows, keyboard scrolling, and three themes.

import json
import sys
import time

from render_scroll import connect

THEMES = ("dark", "sunlight", "red-night")

HOME_MEASURE = """
function measureBandmapHome(expectedTheme) {
  const app = document.querySelector('.app');
  const key = document.querySelector('.statusbar a[href="/bands"]');
  const title = document.querySelector('.statusbar__title');
  const exitBtn = document.querySelector('.btn-exit');
  if (!app || !key || !title || !exitBtn) return null;

  function inViewport(r) {
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }

  const keyRect = key.getBoundingClientRect();
  const exitRect = exitBtn.getBoundingClientRect();
  const exitUnclipped = inViewport(exitRect) &&
                        exitBtn.scrollWidth <= exitBtn.clientWidth + 1 &&
                        exitBtn.scrollHeight <= exitBtn.clientHeight + 1;
  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    bandsKey: [Math.round(keyRect.width * 10) / 10, Math.round(keyRect.height * 10) / 10],
    bandsInViewport: inViewport(keyRect),
    titleUnclipped: title.scrollWidth <= title.clientWidth + 1,
    exitWhole: exitUnclipped,
  };
}
"""

PAGE_MEASURE = """
function measureBandmapPage(expectedTheme) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back[href="/"]');
  const caption = document.querySelector('.bandmap__caption');
  const dial = document.querySelector('.bandmap__dial');
  const entries = document.querySelector('.bandmap__entries');
  const rows = [...document.querySelectorAll('.bandmap__row')];
  const lessonRows = rows.filter(el => !el.classList.contains('bandmap__row--off-dial'));
  const offDialRows = rows.filter(el => el.classList.contains('bandmap__row--off-dial'));
  const replay = document.querySelector('.bandmap__row .logbook-mark--replay');
  if (!app || !back || !caption || !dial || !entries || !rows.length || !replay) return null;

  function inViewport(r) {
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }

  const backRect = back.getBoundingClientRect();
  const capRect = caption.getBoundingClientRect();
  const capStyle = getComputedStyle(caption);
  const capLineH = parseFloat(capStyle.lineHeight) || 24;
  const captionSingleLine = capRect.height <= capLineH * 1.5;
  const captionUnclipped = inViewport(capRect) && caption.scrollWidth <= caption.clientWidth + 1;

  const dialChildren = [...dial.querySelectorAll('rect, line, text')];
  const leftEdge = Math.min(...dialChildren.map(el => el.getBoundingClientRect().left));
  const rightEdge = Math.max(...dialChildren.map(el => el.getBoundingClientRect().right));

  const rects = rows.map(el => el.getBoundingClientRect());
  const minRowH = Math.min(...rects.map(r => r.height));
  let rowsOverlap = false;
  for (let i = 1; i < rects.length; i++) {
    if (rects[i].top < rects[i - 1].bottom - 0.5) {
      rowsOverlap = true;
      break;
    }
  }

  const replayStyle = getComputedStyle(replay);
  const lessonBordersDouble = lessonRows.length > 0 && lessonRows.every(
    el => getComputedStyle(el).borderTopStyle === 'double'
  );
  const offDialBordersDashed = offDialRows.length > 0 && offDialRows.every(
    el => getComputedStyle(el).borderTopStyle === 'dashed'
  );

  const fontEls = [
    caption,
    replay,
    ...document.querySelectorAll(
      '.bandmap__dial-label, .bandmap__heading, .bandmap__line1, .bandmap__line2, ' +
      '.bandmap__no-radio'
    ),
  ];
  const minFontPx = Math.min(...fontEls.map(el => parseFloat(getComputedStyle(el).fontSize) || 0));

  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    back: inViewport(backRect) && backRect.width >= 72 && backRect.height >= 72,
    captionSingleLine,
    captionUnclipped,
    overviewBounds: [Math.round(leftEdge * 10) / 10, Math.round(rightEdge * 10) / 10],
    entriesClientH: entries.clientHeight,
    rowCount: rows.length,
    minRowH: Math.round(minRowH * 10) / 10,
    rowsOverlap,
    replayRadius: replayStyle.borderTopLeftRadius,
    replayText: replay.textContent.trim(),
    lessonBordersDouble,
    offDialBordersDashed,
    minFontPx: Math.round(minFontPx * 10) / 10,
  };
}
"""

END_MEASURE = """
function measureBandmapEnd(expectedTheme) {
  const entries = document.querySelector('.bandmap__entries');
  const offDial = [...document.querySelectorAll('.bandmap__row--off-dial')];
  const lastOff = offDial[offDial.length - 1];
  const noRadio = document.querySelector('.bandmap__no-radio');
  if (!entries || !lastOff || !noRadio) return null;

  const box = entries.getBoundingClientRect();
  function visibleInEntries(el) {
    const r = el.getBoundingClientRect();
    return r.height > 0 && r.top >= box.top - 0.5 && r.bottom <= box.bottom + 1;
  }

  return {
    scrollTop: Math.round(entries.scrollTop),
    lastOffDialVisible: visibleInEntries(lastOff),
    noRadioVisible: visibleInEntries(noRadio),
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


def press_key(browser, key, code, key_code):
    browser.call(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": key, "code": code,
         "windowsVirtualKeyCode": key_code, "nativeVirtualKeyCode": key_code},
    )
    browser.call(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "key": key, "code": code,
         "windowsVirtualKeyCode": key_code, "nativeVirtualKeyCode": key_code},
    )


def verdict(ok, label, measures):
    print(label, json.dumps(measures))
    print(f"VERDICT: {'PASS' if ok else 'FAIL'} - {label}")
    return not ok


def _home_ok(m, theme) -> bool:
    key = m.get("bandsKey") or [0, 0]
    return (
        m.get("theme") == theme
        and m.get("doc") == [800, 480]
        and len(key) == 2
        and key[0] >= 72
        and key[1] >= 72
        and bool(m.get("bandsInViewport"))
        and bool(m.get("titleUnclipped"))
        and bool(m.get("exitWhole"))
    )


def _page_ok(page, end, theme) -> bool:
    bounds = page.get("overviewBounds") or [0, 999]
    return (
        page.get("theme") == theme
        and page.get("doc") == [800, 480]
        and bool(page.get("back"))
        and bool(page.get("captionSingleLine"))
        and bool(page.get("captionUnclipped"))
        and len(bounds) == 2
        and bounds[0] >= 17.5
        and bounds[1] <= 782.5
        and page.get("entriesClientH", 0) >= 280
        and page.get("rowCount", 0) > 0
        and page.get("minRowH", 0) >= 72
        and not page.get("rowsOverlap", True)
        and page.get("replayRadius") == "0px"
        and page.get("replayText") == "REPLAY ONLY"
        and bool(page.get("lessonBordersDouble"))
        and bool(page.get("offDialBordersDashed"))
        and page.get("minFontPx", 0) >= 18
        and end.get("scrollTop", 0) > 0
        and bool(end.get("lastOffDialVisible"))
        and bool(end.get("noRadioVisible"))
    )


def main(console):
    browser = connect()
    try:
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        home_url = f"{console}/"
        bands_url = f"{console}/bands"
        load_page(browser, home_url)
        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            load_page(browser, home_url)
            home = browser.ev(f"{HOME_MEASURE}\nmeasureBandmapHome({json.dumps(theme)})") or {}
            failed |= verdict(_home_ok(home, theme), f"bandmap home {theme}", home)

            load_page(browser, bands_url)
            page = browser.ev(f"{PAGE_MEASURE}\nmeasureBandmapPage({json.dumps(theme)})") or {}
            press_key(browser, "PageDown", "PageDown", 34)
            press_key(browser, "End", "End", 35)
            time.sleep(0.1)
            end = browser.ev(f"{END_MEASURE}\nmeasureBandmapEnd({json.dumps(theme)})") or {}
            failed |= verdict(
                _page_ok(page, end, theme),
                f"bandmap {theme}",
                {"page": page, "end": end},
            )
        return int(failed)
    finally:
        browser.s.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
