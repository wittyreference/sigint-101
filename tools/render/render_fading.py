# ABOUTME: Check M26's faded consolidation pilot at 800x480 over Chromium DevTools in 3 themes.
# ABOUTME: Loads /lesson/26 through the render proxy and renders unsealed stages in isolation.

import json
import sys
import tempfile
import time
from pathlib import Path

from render_scroll import connect, fence, leave_page

RENDER_CHECK = {
    "name": "fading",
    "pages": ("/lesson/26",),
    "scratch": False,
    "order": 140,
}

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

THEMES = ("dark", "sunlight", "red-night")
LESSON_ID = 26

TOOL_MISSING_PAYLOAD = {
    "available": False,
    "cause": "missing_tool",
    "message": "rtl_ais-sigint101 is not installed.",
    "next_step": "Run deploy/provision.sh",
}


def fetch_shim(payload: dict) -> str:
    """Stub /api/ais with the refusal fixture and block state-writing POSTs."""
    body = json.dumps(payload)
    return f"""
(function () {{
  const origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {{
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = ((init && init.method) || 'GET').toUpperCase();
    if (url.indexOf('/api/ais') !== -1) {{
      return Promise.resolve(new Response({json.dumps(body)}, {{
        status: 503,
        headers: {{ 'content-type': 'application/json' }},
      }}));
    }}
    if (method !== 'GET' ||
        url.indexOf('/api/predict') !== -1 ||
        url.indexOf('/api/stage') !== -1 ||
        url.indexOf('/api/opened') !== -1 ||
        url.indexOf('/api/tuning') !== -1 ||
        url.indexOf('/api/complete') !== -1) {{
      return Promise.resolve(new Response('{{"stubbed":true}}', {{
        status: 200,
        headers: {{ 'content-type': 'application/json' }},
      }}));
    }}
    return origFetch(input, init);
  }};
}})();
"""


def _render_unsealed_m26_html(shim: str) -> str:
    """Render real unsealed /lesson/26 HTML in an isolated temp app (zero console writes)."""
    from fastapi.testclient import TestClient

    from sigint101.app import PACK_DIR, create_app
    from sigint101.hardware import RadioProbe

    with tempfile.TemporaryDirectory(prefix="sigint101-fading-html-") as tmp:
        app = create_app(
            pack_dir=PACK_DIR,
            data_dir=Path(tmp),
            probe=lambda: RadioProbe(present=False, known=True, method="lsusb",
                                     detail="render check"),
        )
        with TestClient(app) as client:
            client.post(
                f"/api/predict?lesson_id={LESSON_ID}",
                data={"prediction": "mmsi"},
                follow_redirects=False,
            )
            html = client.get(f"/lesson/{LESSON_ID}").text
    inject = f"<script>{shim}</script>"
    return html.replace("<head>", f"<head>\n{inject}", 1) if "<head>" in html else inject + html


MEASURE_FADING_PREDICT = """
function measureFadingPredict(expectedTheme) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const panel = document.querySelector('.stage-panel[data-stage="predict"]');
  if (!app || !back || !panel) return null;

  function inViewport(r) {
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }
  const chips = [...panel.querySelectorAll('.predict-chip')];
  const commit = panel.querySelector('.predict__actions .btn--primary');
  const axis = panel.querySelector('.decoder__axis');
  const visible = (document.body.innerText || '');
  const html = document.documentElement.outerHTML || '';
  const allControls = commit ? [...chips, commit] : chips;
  const controlsFit = allControls.length > 0 && allControls.every(el => {
    const r = el.getBoundingClientRect();
    return inViewport(r) && r.height >= 72;
  });

  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    back: inViewport(back.getBoundingClientRect()) && back.getBoundingClientRect().height >= 72,
    axisText: axis ? axis.textContent.trim() : '',
    namesBand: /marine\\s+vhf/i.test(visible) && /162\\s*mhz/i.test(visible),
    namesDecoder: /rtl[_\\s-]?ais|aisdecoder/i.test(html),
    controlsFit,
    bottomPx: commit ? Math.round(commit.getBoundingClientRect().bottom) : 0,
  };
}
"""

MEASURE_FADING_STAGE = """
function measureFadingStage(stageName) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const panel = document.querySelector('.stage-panel[data-stage="' + stageName + '"]');
  if (!app || !back || !panel) return null;

  function inViewport(r) {
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }

  const badge = panel.querySelector('[data-ais-badge]');
  const empty = panel.querySelector('[data-ais-empty]');
  const freq = panel.querySelector('[data-faded-freq]');
  const unit = freq ? freq.querySelector('span') : null;
  const strip = panel.querySelector('[data-faded-tune="ais"]');
  const caption = strip ? strip.querySelector('.tune__caption') : null;
  const body = panel.querySelector('.scroll-frame > .stage-panel__body');
  const buttons = strip ? [...strip.querySelectorAll('button')] : [];
  const visibleButtons = buttons.filter(b => getComputedStyle(b).display !== 'none');
  const rects = visibleButtons.map(b => b.getBoundingClientRect());
  const buttonsFit = rects.length > 0 && rects.every(r => inViewport(r) && r.height >= 72);
  let buttonsOverlap = false;
  for (let i = 1; i < rects.length; i++) {
    if (rects[i].left < rects[i - 1].right - 0.5) buttonsOverlap = true;
  }
  const dial = strip ? strip.querySelector('.tune__dial') : null;
  const dialRect = dial ? dial.getBoundingClientRect() : null;
  const dialFits = Boolean(dialRect && inViewport(dialRect) && dialRect.width >= 110);
  const maxRight = Math.max(0, ...rects.map(r => Math.round(r.right)));
  const maxBottom = Math.max(0, ...rects.map(r => Math.round(r.bottom)));

  const fullText = panel.innerText || '';
  const namesDecoder = /rtl[_\\s-]?ais|aisdecoder|provision\\.sh/i.test(fullText);
  const captionFontPx = caption ? parseFloat(getComputedStyle(caption).fontSize) || 0 : 0;
  const unitFontPx = unit ? parseFloat(getComputedStyle(unit).fontSize) || 0 : 0;
  const badgeFontPx = badge ? parseFloat(getComputedStyle(badge).fontSize) || 0 : 0;
  const captionClipped = Boolean(caption && caption.scrollWidth > caption.clientWidth + 1);
  const dialClipped = Boolean(freq && freq.scrollWidth > freq.clientWidth + 1);

  return {
    theme: app.dataset.theme,
    stage: stageName,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    back: inViewport(back.getBoundingClientRect()) && back.getBoundingClientRect().height >= 72,
    badge: badge ? badge.textContent.trim() : '',
    emptyText: empty ? empty.textContent.trim() : '',
    freqText: freq ? freq.textContent.trim() : '',
    dialPending: Boolean(freq && freq.classList.contains('tune__freq--pending')),
    namesDecoder,
    captionFontPx,
    unitFontPx,
    badgeFontPx,
    captionClipped,
    dialClipped,
    buttonsFit,
    buttonsOverlap,
    dialFits,
    dialWidth: dialRect ? Math.round(dialRect.width) : 0,
    narrationH: body ? Math.round(body.clientHeight) : 0,
    maxRight,
    maxBottom,
  };
}
"""

MEASURE_FADING_EXPLAIN = """
function measureFadingExplain(expectedTheme) {
  const app = document.querySelector('.app');
  const panel = document.querySelector('.stage-panel[data-stage="explain"]');
  const complete = panel?.querySelector('[data-complete]');
  if (!app || !panel || !complete) return null;
  const r = complete.getBoundingClientRect();
  const fits = r.width > 0 && r.height >= 72 && r.left >= 0 && r.top >= 0 &&
               r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    completeFits: fits,
    completeBottom: Math.round(r.bottom),
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
            browser.call(
                "Runtime.evaluate",
                {"expression": "document.fonts.ready", "awaitPromise": True},
            )
            return
        time.sleep(0.2)
    raise RuntimeError("page did not finish loading")


def verdict(ok, label, measures):
    print(label, json.dumps(measures))
    print(f"VERDICT: {'PASS' if ok else 'FAIL'} - {label}")
    return not ok


def _predict_ok(m, theme) -> bool:
    return (
        m.get("theme") == theme
        and m.get("doc") == [800, 480]
        and bool(m.get("back"))
        and bool(m.get("namesBand"))
        and not m.get("namesDecoder")
        and bool(m.get("controlsFit"))
    )


def _stage_layout_ok(m, theme) -> bool:
    return (
        m.get("theme") == theme
        and m.get("doc") == [800, 480]
        and bool(m.get("back"))
        and not m.get("namesDecoder")
        and m.get("captionFontPx", 0) >= 18
        and m.get("unitFontPx", 0) >= 18
        and m.get("badgeFontPx", 0) >= 18
        and not m.get("captionClipped")
        and not m.get("dialClipped")
        and bool(m.get("buttonsFit"))
        and not m.get("buttonsOverlap")
        and bool(m.get("dialFits"))
        and m.get("narrationH", 0) >= 58
    )


def _untuned_ok(m, theme) -> bool:
    return (
        _stage_layout_ok(m, theme)
        and bool(m.get("dialPending"))
        and m.get("badge") == "UNTUNED"
        and "Not tuned yet" in (m.get("emptyText") or "")
        and (m.get("freqText") or "").startswith("100.0")
    )


def _offband_ok(m, theme) -> bool:
    return (
        _stage_layout_ok(m, theme)
        and bool(m.get("dialPending"))
        and m.get("badge") == "OFF BAND"
        and "100.0 MHz" in (m.get("emptyText") or "")
        and "162 MHz" in (m.get("emptyText") or "")
    )


def _tuned_ok(m, theme) -> bool:
    return (
        _stage_layout_ok(m, theme)
        and (m.get("freqText") or "").startswith("162.0")
        and m.get("badge") not in ("", "UNTUNED", "OFF BAND", "LIVE")
        and bool(m.get("dialPending"))
        and "Tuned to" not in (m.get("emptyText") or "")
    )


def _stepped_off_ok(m, theme) -> bool:
    return (
        _stage_layout_ok(m, theme)
        and bool(m.get("dialPending"))
        and m.get("badge") == "UNTUNED"
        and "Not tuned yet" in (m.get("emptyText") or "")
        and (m.get("freqText") or "").startswith("164.0")
    )


def _explain_ok(m, theme) -> bool:
    return (
        m.get("theme") == theme
        and m.get("doc") == [800, 480]
        and bool(m.get("completeFits"))
    )


def main(console):
    browser = connect()
    failed = False
    try:
        # connect() has already blocked the whole of /api/* (render_scroll.block_console_api);
        # the proxy refuses /api/* and every write too.
        browser.call("Page.enable")
        browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False},
        )
        lesson_url = f"{console}/lesson/{LESSON_ID}"
        load_page(browser, lesson_url)
        unsealed_html = _render_unsealed_m26_html(fetch_shim(TOOL_MISSING_PAYLOAD))

        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")

            # 1) Pre-predict screen loaded from the proxied console: names only the band, fits.
            load_page(browser, lesson_url)
            pred = browser.ev(
                f"{MEASURE_FADING_PREDICT}\nmeasureFadingPredict({json.dumps(theme)})"
            ) or {}
            failed |= verdict(_predict_ok(pred, theme), f"fading predict {theme}", pred)

            # 2) Unsealed stages rendered offline with shimmed /api/ais (zero writes to console).
            browser.ev(
                f"document.open(); document.write({json.dumps(unsealed_html)}); document.close();"
            )
            browser.call(
                "Runtime.evaluate",
                {"expression": "document.fonts.ready", "awaitPromise": True},
            )
            untuned = browser.ev(
                f"{MEASURE_FADING_STAGE}\n"
                f"measureFadingStage('operate') /* untuned {theme} */"
            ) or {}
            failed |= verdict(
                _untuned_ok(untuned, theme), f"fading operate untuned {theme}", untuned
            )

            # 3) Tap Tune while off-band (100.0 MHz) -> honest OFF BAND refusal.
            browser.ev(
                "document.querySelector('.stage-panel[data-stage=\"operate\"] "
                "[data-faded-apply]')?.click()"
            )
            offband = browser.ev(
                f"{MEASURE_FADING_STAGE}\n"
                f"measureFadingStage('operate') /* offband {theme} */"
            ) or {}
            failed |= verdict(
                _offband_ok(offband, theme), f"fading operate offband {theme}", offband
            )

            # 4) Step to 162.0 MHz (6 x 10 MHz + STEP -> 1 MHz + 2 x 1 MHz) and tap Tune.
            browser.ev(
                "(() => {"
                "  const s = document.querySelector("
                "    '.stage-panel[data-stage=\"operate\"] [data-faded-tune=\"ais\"]'"
                "  );"
                "  if (!s) return;"
                "  const up = s.querySelector('[data-faded-dir=\"1\"]');"
                "  const step = s.querySelector('[data-faded-step]');"
                "  const apply = s.querySelector('[data-faded-apply]');"
                "  for (let i = 0; i < 6; i++) up.click();"
                "  step.click();"
                "  for (let i = 0; i < 2; i++) up.click();"
                "  apply.click();"
                "})()"
            )
            time.sleep(0.2)
            tuned = browser.ev(
                f"{MEASURE_FADING_STAGE}\n"
                f"measureFadingStage('operate') /* tuned {theme} */"
            ) or {}
            failed |= verdict(_tuned_ok(tuned, theme), f"fading operate tuned {theme}", tuned)

            # 4b) F1: step dial off-band (+2 MHz -> 164.0 MHz) after Tune -> drops to UNTUNED.
            browser.ev(
                "(() => {"
                "  const s = document.querySelector("
                "    '.stage-panel[data-stage=\"operate\"] [data-faded-tune=\"ais\"]'"
                "  );"
                "  if (!s) return;"
                "  const up = s.querySelector('[data-faded-dir=\"1\"]');"
                "  up.click(); up.click();"
                "})()"
            )
            stepped_off = browser.ev(
                f"{MEASURE_FADING_STAGE}\n"
                f"measureFadingStage('operate') /* stepped_off {theme} */"
            ) or {}
            failed |= verdict(
                _stepped_off_ok(stepped_off, theme),
                f"fading operate stepped-off {theme}",
                stepped_off,
            )

            # Step back to 162.0 MHz and re-apply Tune before checking observe.
            browser.ev(
                "(() => {"
                "  const s = document.querySelector("
                "    '.stage-panel[data-stage=\"operate\"] [data-faded-tune=\"ais\"]'"
                "  );"
                "  if (!s) return;"
                "  const dn = s.querySelector('[data-faded-dir=\"-1\"]');"
                "  const apply = s.querySelector('[data-faded-apply]');"
                "  dn.click(); dn.click(); apply.click();"
                "})()"
            )
            time.sleep(0.2)

            # 5) Advance to observe (tightest row: Prev + Next step beside tune controls).
            browser.ev(
                "document.querySelector('.stage-panel[data-stage=\"operate\"] "
                "[data-nav=\"next\"]')?.click()"
            )
            observe = browser.ev(
                f"{MEASURE_FADING_STAGE}\n"
                f"measureFadingStage('observe') /* observe {theme} */"
            ) or {}
            failed |= verdict(_stage_layout_ok(observe, theme), f"fading observe {theme}", observe)

            # 6) Advance to explain and check Complete lesson button fits 800x480.
            browser.ev(
                "document.querySelector('.stage-panel[data-stage=\"observe\"] "
                "[data-nav=\"next\"]')?.click()"
            )
            explain = browser.ev(
                f"{MEASURE_FADING_EXPLAIN}\nmeasureFadingExplain({json.dumps(theme)})"
            ) or {}
            failed |= verdict(_explain_ok(explain, theme), f"fading explain {theme}", explain)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
