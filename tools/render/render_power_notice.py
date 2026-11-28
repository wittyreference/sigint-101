# ABOUTME: Check the .power-notice banner at 800x480 across all three themes over CDP.
# ABOUTME: Verifies countdown, failed+retry, hide-on-clear, Back clearance, and z-index > figure.

import json
import sys
import time

from render_scroll import connect, fence, leave_page

RENDER_CHECK = {
    "name": "power-notice",
    "pages": ("/", "/lesson/1", "/lesson/29", "/exam/review/T6C02"),
    "scratch": False,
    "order": 130,
}

THEMES = ("dark", "sunlight", "red-night")
ROUTES = (
    ("home", "/"),
    ("lesson1", "/lesson/1"),
    ("predict29", "/lesson/29"),
    ("figure", "/exam/review/T6C02"),
)

COUNTDOWN_PAYLOAD = {
    "active": True,
    "failed": False,
    "remaining_s": 42,
    "voltage": 9.21,
    "threshold_v": 9.5,
}
FAILED_PAYLOAD = {
    "active": True,
    "failed": True,
    "retry_in_s": 47,
    "voltage": 9.18,
    "threshold_v": 9.5,
}
INACTIVE_PAYLOAD = {"active": False}

MEASURE_POWER_NOTICE = """
async function measurePowerNotice(
  expectedTheme, checkFigure, countdownPayload, failedPayload, inactivePayload
) {
  const app = document.querySelector('.app');
  const bar = document.querySelector('.statusbar');
  const back = document.querySelector('.btn-back, .btn-exit');
  const banner = document.querySelector('[data-power-notice]');
  if (!app || !bar || !back || !banner) return null;

  const initialHidden = banner.hidden === true;
  const roleAlert = banner.getAttribute('role') === 'alert' &&
                    banner.getAttribute('aria-live') === 'assertive';

  let currentPayload = inactivePayload;
  window.fetch = function (input) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.indexOf('/api/power') !== -1) {
      return Promise.resolve(new Response(JSON.stringify(currentPayload), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }));
    }
    return Promise.reject(new Error('blocked in render check'));
  };

  async function applyAndMeasure(payload) {
    currentPayload = payload;
    document.dispatchEvent(new CustomEvent('sigint101:power-poll'));
    await new Promise(r => setTimeout(r, 30));
    const bRect = banner.getBoundingClientRect();
    const backRect = back.getBoundingClientRect();
    const barRect = bar.getBoundingClientRect();
    const cs = getComputedStyle(banner);
    const appCs = getComputedStyle(app);
    const headline = banner.querySelector('[data-power-headline]');
    const detail = banner.querySelector('[data-power-detail]');
    const glyph = banner.querySelector('.power-notice__glyph');
    const hCs = headline ? getComputedStyle(headline) : null;
    const dCs = detail ? getComputedStyle(detail) : null;
    const hRect = headline ? headline.getBoundingClientRect() : null;
    const dRect = detail ? detail.getBoundingClientRect() : null;
    return {
      hidden: banner.hidden,
      failedClass: banner.classList.contains('power-notice--failed'),
      doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
      barBottom: Math.round(barRect.bottom),
      backH: Math.round(backRect.height),
      backBottom: Math.round(backRect.bottom),
      backInView: backRect.left >= 0 && backRect.top >= 0 &&
                  backRect.right <= innerWidth + 0.5 && backRect.bottom <= innerHeight + 0.5,
      top: Math.round(bRect.top),
      bottom: Math.round(bRect.bottom),
      height: Math.round(bRect.height),
      right: Math.round(bRect.right),
      zIndex: parseInt(cs.zIndex, 10) || 0,
      invertedBg: cs.backgroundColor !== appCs.backgroundColor,
      borderBottom: cs.borderBottomStyle + ':' + cs.borderBottomWidth,
      hasGlyph: !!glyph && glyph.getBoundingClientRect().width >= 20,
      headlinePx: hCs ? parseFloat(hCs.fontSize) : 0,
      detailPx: dCs ? parseFloat(dCs.fontSize) : 0,
      headlineText: headline ? headline.textContent.trim() : '',
      detailText: detail ? detail.textContent.trim() : '',
      noOverflow: banner.scrollHeight <= banner.clientHeight + 1 &&
                  banner.scrollWidth <= banner.clientWidth + 1 &&
                  !!hRect && !!dRect &&
                  dRect.bottom <= bRect.bottom + 0.5 &&
                  detail.scrollWidth <= detail.clientWidth + 1,
    };
  }

  const cd = await applyAndMeasure(countdownPayload);
  const fl = await applyAndMeasure(failedPayload);

  let aboveFigure = true;
  if (checkFigure) {
    const fig = document.querySelector('.exam-figure');
    if (!fig) {
      aboveFigure = false;
    } else {
      location.hash = '#' + fig.id;
      await new Promise(r => setTimeout(r, 30));
      const openFig = document.querySelector('.exam-figure:target');
      if (!openFig) {
        aboveFigure = false;
      } else {
        const figZ = parseInt(getComputedStyle(openFig).zIndex, 10) || 0;
        const noticeZ = parseInt(getComputedStyle(banner).zIndex, 10) || 0;
        const bRect = banner.getBoundingClientRect();
        aboveFigure = noticeZ > figZ && !banner.hidden && bRect.height > 0 && bRect.height <= 96;
      }
      location.hash = '';
    }
  }

  const clr = await applyAndMeasure(inactivePayload);

  const countdownOk = (
    cd.hidden === false &&
    cd.failedClass === false &&
    cd.doc[0] === 800 && cd.doc[1] === 480 &&
    cd.backInView && cd.backH >= 72 && cd.backBottom <= cd.top + 1 &&
    cd.top >= 74 && cd.height > 0 && cd.height <= 96 && cd.right <= 800 &&
    cd.invertedBg && cd.hasGlyph &&
    cd.headlinePx >= 20 && cd.detailPx >= 18 &&
    cd.headlineText.includes('LOW BATTERY') &&
    cd.headlineText.includes('42 s') &&
    cd.noOverflow
  );

  const failedOk = (
    fl.hidden === false &&
    fl.failedClass === true &&
    fl.doc[0] === 800 && fl.doc[1] === 480 &&
    fl.backInView && fl.backH >= 72 && fl.backBottom <= fl.top + 1 &&
    fl.top >= 74 && fl.height > 0 && fl.height <= 96 && fl.right <= 800 &&
    fl.invertedBg && fl.hasGlyph &&
    fl.headlinePx >= 20 && fl.detailPx >= 18 &&
    fl.headlineText.includes('AUTOMATIC POWER-OFF FAILED') &&
    fl.headlineText.includes('RETRYING IN 47 s') &&
    fl.borderBottom !== cd.borderBottom &&
    fl.noOverflow
  );

  return {
    theme: app.dataset.theme,
    doc: cd.doc,
    roleAlert,
    initialHidden,
    countdownOk,
    failedOk,
    hiddenAfterClear: clr.hidden === true,
    aboveFigure,
    countdownH: cd.height,
    failedH: fl.height,
    noticeTop: cd.top,
    backBottom: cd.backBottom,
    headlinePx: cd.headlinePx,
    detailPx: cd.detailPx,
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


def _eval_async(browser, expr):
    res = browser.call(
        "Runtime.evaluate",
        {"expression": expr, "returnByValue": True, "awaitPromise": True},
    )
    return res.get("result", {}).get("value")


def main(console):
    browser = connect()
    try:
        browser.call("Page.enable")
        browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False},
        )
        # Seed origin once so localStorage can switch themes cleanly.
        load_page(browser, f"{console}/")

        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            for name, route in ROUTES:
                load_page(browser, f"{console}{route}")
                check_figure = name == "figure"
                expr = (
                    f"{MEASURE_POWER_NOTICE}\n"
                    f"measurePowerNotice({json.dumps(theme)}, {json.dumps(check_figure)}, "
                    f"{json.dumps(COUNTDOWN_PAYLOAD)}, {json.dumps(FAILED_PAYLOAD)}, "
                    f"{json.dumps(INACTIVE_PAYLOAD)})"
                )
                m = _eval_async(browser, expr) or {}
                ok = (
                    m.get("theme") == theme
                    and m.get("doc") == [800, 480]
                    and m.get("roleAlert") is True
                    and m.get("initialHidden") is True
                    and m.get("countdownOk") is True
                    and m.get("failedOk") is True
                    and m.get("hiddenAfterClear") is True
                    and m.get("aboveFigure") is True
                )
                failed |= verdict(ok, f"power-notice {name} {theme}", m)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
