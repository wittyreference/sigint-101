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
    "reason": "rtl_ais-sigint101 is not installed, so SIGINT 101 cannot decode AIS",
    "message": "rtl_ais-sigint101 is not installed.",
    "next_step": "Run deploy/provision.sh",
}

CAPABILITY_TIMEOUT_PAYLOAD = {
    "available": False,
    "cause": "unavailable",
    "reason": "rtl_ais-sigint101 --sigint101-capability timed out; run deploy/provision.sh",
}

REJECTED_DATAGRAM_PAYLOAD = {
    "available": False,
    "cause": "stopped",
    "state": "stopped",
    "reason": "rtl_ais-sigint101 sent an unreadable datagram (truncated); run deploy/provision.sh",
    "retry_in": 4,
}

NO_RADIO_PAYLOAD = {
    "available": False,
    "cause": "no_radio",
    "reason": "no RTL-SDR on the USB bus — SIGINT 101 will not fake a vessel",
    "message": "No RTL-SDR on the USB bus.",
    "next_step": "Plug in the RTL-SDR.",
}


def fetch_shim(payload: dict) -> str:
    """Stub /api/ais (immediate or deferred) and block state-writing POSTs."""
    return f"""
(function () {{
  const origFetch = window.fetch.bind(window);
  const origSetTimeout = window.setTimeout.bind(window);
  let nextStatus = 503;
  let nextPayload = {json.dumps(payload)};
  let deferred = false;
  let pendingResolve = null;
  let fastTimers = false;
  const state = {{
    aisRequests: 0,
    setResponse(status, body) {{
      deferred = false;
      pendingResolve = null;
      nextStatus = status;
      nextPayload = body;
    }},
    deferNext() {{
      deferred = true;
      pendingResolve = null;
    }},
    hasPending() {{
      return typeof pendingResolve === 'function';
    }},
    resolveNext(status, body) {{
      if (typeof pendingResolve !== 'function') return false;
      const fn = pendingResolve;
      pendingResolve = null;
      fn(new Response(JSON.stringify(body), {{
        status,
        headers: {{ 'content-type': 'application/json' }},
      }}));
      return true;
    }},
    setFastTimers(on) {{
      fastTimers = Boolean(on);
    }},
  }};
  window.__fadingTest = state;

  window.setTimeout = function (fn, ms) {{
    const args = Array.prototype.slice.call(arguments, 2);
    const delay = fastTimers && typeof ms === 'number' && ms >= 500 ? 40 : ms;
    return origSetTimeout.apply(window, [fn, delay].concat(args));
  }};

  window.fetch = function (input, init) {{
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = ((init && init.method) || 'GET').toUpperCase();
    if (url.indexOf('/api/ais') !== -1) {{
      state.aisRequests += 1;
      if (deferred) {{
        return new Promise(function (resolve) {{
          pendingResolve = resolve;
        }});
      }}
      return Promise.resolve(new Response(JSON.stringify(nextPayload), {{
        status: nextStatus,
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

  function textBoundsFit(el, container) {
    if (!el || !container) return false;
    const box = el.getBoundingClientRect();
    const cRect = container.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(el);
    const tr = range.getBoundingClientRect();
    return (
      inViewport(box) &&
      inViewport(tr) &&
      el.scrollWidth <= el.clientWidth + 1 &&
      tr.left >= cRect.left - 1 &&
      tr.right <= cRect.right + 1 &&
      tr.top >= cRect.top - 1 &&
      tr.bottom <= cRect.bottom + 1
    );
  }

  const head = panel.querySelector('[data-ais-faded] .decoder__head');
  const title = panel.querySelector('[data-ais-faded] .decoder__title');
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
  const namesDecoder = (
    /rtl[_\\s-]?ais|aisdecoder|provision\\.sh|--sigint101-capability/i.test(fullText)
  );
  const captionFontPx = caption ? parseFloat(getComputedStyle(caption).fontSize) || 0 : 0;
  const unitFontPx = unit ? parseFloat(getComputedStyle(unit).fontSize) || 0 : 0;
  const titleFontPx = title ? parseFloat(getComputedStyle(title).fontSize) || 0 : 0;
  const badgeFontPx = badge ? parseFloat(getComputedStyle(badge).fontSize) || 0 : 0;
  const captionClipped = Boolean(caption && caption.scrollWidth > caption.clientWidth + 1);
  const dialClipped = Boolean(freq && freq.scrollWidth > freq.clientWidth + 1);
  const titleFits = textBoundsFit(title, head);
  const badgeFits = textBoundsFit(badge, head);
  const titleBox = title ? title.getBoundingClientRect() : null;
  const badgeBox = badge ? badge.getBoundingClientRect() : null;
  const headOverlap = Boolean(
    titleBox && badgeBox &&
    titleBox.right > badgeBox.left + 0.5 &&
    titleBox.bottom > badgeBox.top + 0.5 &&
    badgeBox.bottom > titleBox.top + 0.5
  );

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
    aisRequests: window.__fadingTest ? window.__fadingTest.aisRequests : 0,
    captionFontPx,
    unitFontPx,
    titleFontPx,
    badgeFontPx,
    captionClipped,
    dialClipped,
    titleFits,
    badgeFits,
    headOverlap,
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


def _write_unsealed(browser, unsealed_html: str) -> None:
    browser.ev("window.dispatchEvent(new Event('pagehide'));")
    browser.ev(
        f"document.open(); document.write({json.dumps(unsealed_html)}); document.close();"
    )
    browser.call(
        "Runtime.evaluate",
        {"expression": "document.fonts.ready", "awaitPromise": True},
    )


def _wait_pending_fetch(browser) -> None:
    for _ in range(40):
        pending = browser.ev("Boolean(window.__fadingTest && window.__fadingTest.hasPending())")
        if pending is not False:
            return
        time.sleep(0.025)


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
        and m.get("titleFontPx", 0) >= 18
        and m.get("badgeFontPx", 0) >= 18
        and not m.get("captionClipped")
        and not m.get("dialClipped")
        and bool(m.get("titleFits"))
        and bool(m.get("badgeFits"))
        and not m.get("headOverlap")
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
        and m.get("aisRequests") == 0
    )


def _offband_ok(m, theme) -> bool:
    return (
        _stage_layout_ok(m, theme)
        and bool(m.get("dialPending"))
        and m.get("badge") == "OFF BAND"
        and "100.0 MHz" in (m.get("emptyText") or "")
        and "162 MHz" in (m.get("emptyText") or "")
        and m.get("aisRequests") == 0
    )


def _rejected_moved_ok(m, theme) -> bool:
    return (
        _stage_layout_ok(m, theme)
        and bool(m.get("dialPending"))
        and m.get("badge") == "UNTUNED"
        and "Not tuned yet" in (m.get("emptyText") or "")
        and (m.get("freqText") or "").startswith("162.0")
        and m.get("aisRequests") == 0
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


def _settle_starting_ok(m) -> bool:
    return (
        _stage_layout_ok(m, "dark")
        and (m.get("freqText") or "").startswith("162.0")
        and m.get("badge") == "STARTING"
        and bool(m.get("dialPending"))
    )


def _settle_running_ok(m) -> bool:
    return (
        _stage_layout_ok(m, "dark")
        and (m.get("freqText") or "").startswith("162.0")
        and m.get("badge") == "LIVE"
        and m.get("dialPending") is False
    )


def _settle_stopped_ok(m) -> bool:
    return (
        _stage_layout_ok(m, "dark")
        and (m.get("freqText") or "").startswith("162.0")
        and m.get("badge") == "STOPPED"
        and bool(m.get("dialPending"))
        and "retrying in 4 s" in (m.get("emptyText") or "")
    )


def _refusal_ok(m, badge: str, must_contain: str) -> bool:
    return (
        _stage_layout_ok(m, "dark")
        and (m.get("freqText") or "").startswith("162.0")
        and m.get("badge") == badge
        and bool(m.get("dialPending"))
        and must_contain in (m.get("emptyText") or "")
    )


def _step_to_162_js() -> str:
    return (
        "(() => {"
        "  const s = document.querySelector("
        "    '.stage-panel[data-stage=\"operate\"] [data-faded-tune=\"ais\"]'"
        "  );"
        "  if (!s) return;"
        "  const up = s.querySelector('[data-faded-dir=\"1\"]');"
        "  const step = s.querySelector('[data-faded-step]');"
        "  for (let i = 0; i < 6; i++) up.click();"
        "  step.click();"
        "  for (let i = 0; i < 2; i++) up.click();"
        "})()"
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
            _write_unsealed(browser, unsealed_html)
            untuned = browser.ev(
                f"{MEASURE_FADING_STAGE}\n"
                f"measureFadingStage('operate') /* untuned {theme} */"
            ) or {}
            failed |= verdict(
                _untuned_ok(untuned, theme), f"fading operate untuned {theme}", untuned
            )

            # 3) Tap Tune while off-band (100.0 MHz) -> honest OFF BAND refusal, 0 AIS requests.
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

            # 3b) F1a: step to 162.0 MHz without tapping Tune -> returns to UNTUNED, 0 requests.
            browser.ev(_step_to_162_js())
            rejected_moved = browser.ev(
                f"{MEASURE_FADING_STAGE}\n"
                f"measureFadingStage('operate') /* rejected_moved {theme} */"
            ) or {}
            failed |= verdict(
                _rejected_moved_ok(rejected_moved, theme),
                f"fading operate rejected-moved {theme}",
                rejected_moved,
            )

            # 4) Tap Tune at 162.0 MHz -> honest TOOL MISSING refusal, dial stays pending.
            browser.ev(
                "document.querySelector('.stage-panel[data-stage=\"operate\"] "
                "[data-faded-apply]')?.click()"
            )
            time.sleep(0.15)
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
            time.sleep(0.15)

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

        # 7) F1b: deferred starting -> running and starting -> stopped, checking dial on each poll.
        browser.ev("localStorage.setItem('sigint101.theme', 'dark')")
        _write_unsealed(browser, unsealed_html)
        browser.ev("window.__fadingTest.setFastTimers(true); window.__fadingTest.deferNext();")
        browser.ev(_step_to_162_js())
        browser.ev(
            "document.querySelector('.stage-panel[data-stage=\"operate\"] "
            "[data-faded-apply]')?.click()"
        )
        _wait_pending_fetch(browser)
        browser.ev(
            "window.__fadingTest.resolveNext(200, "
            "{available: true, state: 'starting', vessels: []})"
        )
        _wait_pending_fetch(browser)
        settle_starting = browser.ev(
            f"{MEASURE_FADING_STAGE}\nmeasureFadingStage('operate') /* settle_starting */"
        ) or {}
        failed |= verdict(
            _settle_starting_ok(settle_starting), "fading settle starting", settle_starting
        )

        browser.ev(
            "window.__fadingTest.resolveNext(200, {available: true, state: 'running', vessels: []})"
        )
        _wait_pending_fetch(browser)
        settle_running = browser.ev(
            f"{MEASURE_FADING_STAGE}\nmeasureFadingStage('operate') /* settle_running */"
        ) or {}
        failed |= verdict(
            _settle_running_ok(settle_running), "fading settle running", settle_running
        )

        # Reset and verify starting -> stopped keeps the dial pending.
        _write_unsealed(browser, unsealed_html)
        browser.ev("window.__fadingTest.setFastTimers(true); window.__fadingTest.deferNext();")
        browser.ev(_step_to_162_js())
        browser.ev(
            "document.querySelector('.stage-panel[data-stage=\"operate\"] "
            "[data-faded-apply]')?.click()"
        )
        _wait_pending_fetch(browser)
        browser.ev(
            "window.__fadingTest.resolveNext(200, "
            "{available: true, state: 'starting', vessels: []})"
        )
        _wait_pending_fetch(browser)
        settle_stopped_starting = browser.ev(
            f"{MEASURE_FADING_STAGE}\nmeasureFadingStage('operate') /* settle_stopped_starting */"
        ) or {}
        failed |= verdict(
            _settle_starting_ok(settle_stopped_starting),
            "fading settle stopped-starting",
            settle_stopped_starting,
        )

        browser.ev(
            f"window.__fadingTest.resolveNext(503, {json.dumps(REJECTED_DATAGRAM_PAYLOAD)})"
        )
        _wait_pending_fetch(browser)
        settle_stopped = browser.ev(
            f"{MEASURE_FADING_STAGE}\nmeasureFadingStage('operate') /* settle_stopped */"
        ) or {}
        failed |= verdict(
            _settle_stopped_ok(settle_stopped), "fading settle stopped", settle_stopped
        )

        # 8) F2: all four real refusal shapes show learner-facing copy and real badges/retry time.
        refusals = (
            ("capability_timeout", "capability-timeout", CAPABILITY_TIMEOUT_PAYLOAD,
             "UNAVAILABLE", "unavailable"),
            ("rejected_datagram", "rejected-datagram", REJECTED_DATAGRAM_PAYLOAD,
             "STOPPED", "retrying in 4 s"),
            ("missing_tool", "missing-tool", TOOL_MISSING_PAYLOAD,
             "TOOL MISSING", "Field card"),
            ("no_radio", "no-radio", NO_RADIO_PAYLOAD,
             "NO RADIO", "RTL-SDR"),
        )
        for tag, label, payload, badge, must_contain in refusals:
            _write_unsealed(browser, unsealed_html)
            browser.ev(f"window.__fadingTest.setResponse(503, {json.dumps(payload)});")
            browser.ev(_step_to_162_js())
            browser.ev(
                "document.querySelector('.stage-panel[data-stage=\"operate\"] "
                "[data-faded-apply]')?.click()"
            )
            time.sleep(0.15)
            meas = browser.ev(
                f"{MEASURE_FADING_STAGE}\nmeasureFadingStage('operate') /* refusal_{tag} */"
            ) or {}
            failed |= verdict(
                _refusal_ok(meas, badge, must_contain),
                f"fading refusal {label}",
                meas,
            )

        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
