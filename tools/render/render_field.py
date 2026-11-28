# ABOUTME: Check the rack FIELD target and /field layout at 800x480 over Chromium DevTools.
# ABOUTME: Verify FIELD target, unclipped title, Back, scrolling, and long-command wrap in 3 themes.

import json
import sys
import time

from render_scroll import connect

THEMES = ("dark", "sunlight", "red-night")

# Longest real command shape (SSTV pipeline, sstv.py:106-122), injected only in the browser DOM
# for layout measurement; the page never serves it.
LONG_COMMAND_SAMPLE = (
    "rtl_fm -M fm -f 437550000 -s 48000 -g 49 -d 0 - | "
    "sox -t raw -r 48000 -e signed-integer -b 16 -c 1 - -t wav "
    "/tmp/sstv-capture-20261128T000000.wav trim 0 120 | "
    "sstv -d /tmp/sstv-capture-20261128T000000.wav "
    "-o /var/lib/sigint101/data/sstv/20261128T000000-000-r1.png"
)

RACK_FIELD = """
function measureRackField(expectedTheme) {
  const key = document.querySelector('a[href="/field"]');
  const title = document.querySelector('.statusbar__title');
  const rect = key?.getBoundingClientRect();
  if (!rect || !title) return null;
  function inViewport(r) {
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth && r.bottom <= innerHeight;
  }
  return {
    theme: document.querySelector('.app').dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    target: [rect.width, rect.height],
    visible: inViewport(rect),
    titleUnclipped: title.scrollWidth <= title.clientWidth,
  };
}
"""

FIELD_PAGE = """
function measureFieldPage(expectedTheme, longCmd) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back[href="/operate"]');
  const list = document.querySelector('.field__entries');
  const firstEntry = list?.querySelector('.field__entry');
  const last = list?.querySelector('.field__entry:last-child');
  if (!app || !back || !list || !firstEntry || !last) return null;

  // Inject the longest real command shape into a row for wrap/width verification only.
  const probe = document.createElement('p');
  probe.className = 'field__cmd';
  const code = document.createElement('code');
  code.textContent = longCmd;
  probe.appendChild(code);
  firstEntry.appendChild(probe);

  function inViewport(element) {
    const r = element.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth && r.bottom <= innerHeight;
  }

  const bodyPx = parseFloat(getComputedStyle(document.querySelector('.field')).fontSize);
  const codePx = parseFloat(getComputedStyle(code).fontSize);
  const scrolls = getComputedStyle(list).overflowY === 'auto' &&
                  list.scrollHeight > list.clientHeight && list.scrollWidth <= list.clientWidth;
  const noOverflow = list.scrollWidth <= list.clientWidth &&
                     firstEntry.scrollWidth <= firstEntry.clientWidth &&
                     probe.scrollWidth <= probe.clientWidth;
  const backBefore = inViewport(back);
  list.scrollTop = list.scrollHeight;
  const end = last.getBoundingClientRect().bottom;
  const lastVisible = list.scrollTop > 0 && end <= list.getBoundingClientRect().bottom + 1;
  probe.remove();

  return {
    theme: app.dataset.theme,
    back: backBefore && inViewport(back) && back.getBoundingClientRect().height >= 72,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    scrolls,
    lastVisible,
    noOverflow,
    bodyPx,
    codePx,
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
        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            for inst in ("ism", "waterfall"):
                load_page(browser, f"{console}/operate/{inst}")
                rack = browser.ev(f"{RACK_FIELD}\nmeasureRackField({json.dumps(theme)})") or {}
                rack_ok = (rack.get("theme") == theme and rack.get("doc") == [800, 480]
                           and rack.get("visible") and rack.get("titleUnclipped")
                           and all(72 <= size <= 80 for size in rack.get("target", ())))
                failed |= verdict(rack_ok, f"rack-field-{inst} {theme}", rack)

            load_page(browser, f"{console}/field")
            measures = browser.ev(
                f"{FIELD_PAGE}\nmeasureFieldPage({json.dumps(theme)}, "
                f"{json.dumps(LONG_COMMAND_SAMPLE)})"
            ) or {}
            page_ok = (measures.get("theme") == theme and measures.get("doc") == [800, 480]
                       and measures.get("back") and measures.get("scrolls")
                       and measures.get("lastVisible") and measures.get("noOverflow")
                       and measures.get("bodyPx", 0) >= 18 and measures.get("codePx", 0) >= 18)
            failed |= verdict(page_ok, f"field {theme}", measures)
        return int(failed)
    finally:
        browser.s.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
