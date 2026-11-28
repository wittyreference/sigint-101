# ABOUTME: Check the closing lesson (/lesson/40) layout at 800x480 across all three themes over CDP.
# ABOUTME: Verifies predict fit, operate command box wrap/font, observe, and explain Complete fit.

import json
import sys
import tempfile
import time
from pathlib import Path

from render_scroll import connect, fence, leave_page

RENDER_CHECK = {
    "name": "closing",
    "pages": ("/lesson/40",),
    "scratch": False,
    "order": 120,
}

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

THEMES = ("dark", "sunlight", "red-night")
SAMPLE_M10_CMD = "rtl_433 -f 433920000 -F json -M level -d 0"


def _render_unsealed_l40_html() -> str:
    """Render unsealed /lesson/40 HTML in an isolated temp app (zero live console writes)."""
    from fastapi.testclient import TestClient

    from sigint101.app import create_app
    from sigint101.events import log_event
    from sigint101.hardware import RadioProbe

    with tempfile.TemporaryDirectory(prefix="sigint101-closing-html-") as tmp:
        data_dir = Path(tmp)
        log_event(
            data_dir / "events.jsonl",
            "decode_start",
            {"command": SAMPLE_M10_CMD, "command_from": "process"},
            clock=lambda: 1_700_000_300.0,
        )
        app = create_app(
            pack_dir=REPO / "lesson_packs",
            data_dir=data_dir,
            probe=lambda: RadioProbe(False, "lsusb", "render check"),
        )
        with TestClient(app) as client:
            client.post(
                "/api/predict?lesson_id=40",
                data={"prediction": "radio-busy"},
                follow_redirects=False,
            )
            return client.get("/lesson/40").text


CLOSING_PREDICT = """
function measureClosingPredict(expectedTheme) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const panel = document.querySelector('.stage-panel[data-stage="predict"]');
  if (!app || !back || !panel) return null;
  document.querySelectorAll('.stage-panel').forEach(p => { p.hidden = (p !== panel); });

  function inViewport(r) {
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }
  function unclipped(el) {
    const r = el.getBoundingClientRect();
    if (!inViewport(r)) return false;
    const cs = getComputedStyle(el);
    if (cs.overflowX !== 'visible' && el.scrollWidth > el.clientWidth + 1) return false;
    if (cs.overflowY !== 'visible' && el.scrollHeight > el.clientHeight + 1) return false;
    return true;
  }

  const chips = [...panel.querySelectorAll('.predict-chip')];
  const commit = panel.querySelector('.btn-commit, .btn--primary');
  const backRect = back.getBoundingClientRect();
  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    back: inViewport(backRect) && backRect.height >= 72,
    chipsFit: chips.length >= 3 && chips.every(c => unclipped(c)),
    commitFit: !!commit && unclipped(commit) && commit.getBoundingClientRect().height >= 72,
    commitBottom: commit ? Math.round(commit.getBoundingClientRect().bottom) : 0,
  };
}
"""

CLOSING_UNSEALED = """
function measureClosingUnsealed(expectedTheme, expectedCmd) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const panels = [...document.querySelectorAll('.stage-panel')];
  const operate = document.querySelector('.stage-panel[data-stage="operate"]');
  const observe = document.querySelector('.stage-panel[data-stage="observe"]');
  const explain = document.querySelector('.stage-panel[data-stage="explain"]');
  if (!app || !back || !operate || !observe || !explain) return null;

  function inViewport(r) {
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }
  function unclipped(el) {
    const r = el.getBoundingClientRect();
    if (!inViewport(r)) return false;
    const cs = getComputedStyle(el);
    if (cs.overflowX !== 'visible' && el.scrollWidth > el.clientWidth + 1) return false;
    if (cs.overflowY !== 'visible' && el.scrollHeight > el.clientHeight + 1) return false;
    return true;
  }
  function footOk(panel) {
    const btns = [...panel.querySelectorAll('.stage-foot button')];
    return btns.length > 0 && btns.every(
      b => unclipped(b) && b.getBoundingClientRect().height >= 72
    );
  }

  panels.forEach(p => { p.hidden = (p !== operate); });
  const box = operate.querySelector('[data-field-lesson="ism"]');
  const code = box ? box.querySelector('.field__cmd code') : null;
  const opBody = operate.querySelector('.stage-panel__body');
  const opDoc = [document.documentElement.scrollWidth, document.documentElement.scrollHeight];
  const backRect = back.getBoundingClientRect();
  const backOk = inViewport(backRect) && backRect.height >= 72;
  const operateFoot = footOk(operate);
  const codePx = code ? parseFloat(getComputedStyle(code).fontSize) : 0;
  const noOverflow = !!opBody && !!box &&
                     opBody.scrollWidth <= opBody.clientWidth + 1 &&
                     box.scrollWidth <= box.clientWidth + 1;
  let cmdVisibleAtTop = false;
  if (opBody && box && code) {
    opBody.scrollTop = 0;
    const bodyRect = opBody.getBoundingClientRect();
    const boxRect = box.getBoundingClientRect();
    const codeRect = code.getBoundingClientRect();
    cmdVisibleAtTop =
      boxRect.top >= bodyRect.top - 0.5 &&
      codeRect.top >= bodyRect.top - 0.5 &&
      codeRect.bottom <= bodyRect.bottom + 0.5 &&
      codeRect.bottom <= innerHeight + 0.5;
  }
  let noSplitHyphens = !!code && code.textContent === expectedCmd;
  if (code) {
    const rowWidth = code.parentElement.clientWidth;
    for (const span of code.querySelectorAll('.field__tok')) {
      if (!span.textContent.includes('-')) continue;
      const range = document.createRange();
      range.selectNodeContents(span);
      if (range.getClientRects().length > 1 && span.getBoundingClientRect().width < rowWidth - 1) {
        noSplitHyphens = false;
      }
    }
  }

  panels.forEach(p => { p.hidden = (p !== observe); });
  const observeFoot = footOk(observe);

  panels.forEach(p => { p.hidden = (p !== explain); });
  const complete = explain.querySelector('[data-complete]');
  const explainFoot = footOk(explain);
  const completeVisible = !!complete && unclipped(complete) &&
                          complete.getBoundingClientRect().height >= 72;

  return {
    theme: app.dataset.theme,
    doc: opDoc,
    back: backOk,
    codePx,
    noOverflow,
    cmdVisibleAtTop,
    noSplitHyphens,
    operateFoot,
    observeFoot,
    explainFoot,
    completeVisible,
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
        # connect() has already blocked the whole of /api/* (render_scroll.block_console_api),
        # /api/predict, /api/stage and /api/complete with the rest; the proxy refuses them too.
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        lesson_url = f"{console}/lesson/40"
        load_page(browser, lesson_url)
        unsealed_html = _render_unsealed_l40_html()

        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            load_page(browser, lesson_url)
            pred = browser.ev(
                f"{CLOSING_PREDICT}\nmeasureClosingPredict({json.dumps(theme)})"
            ) or {}
            pred_ok = (
                pred.get("theme") == theme
                and pred.get("doc") == [800, 480]
                and pred.get("back") is True
                and pred.get("chipsFit") is True
                and pred.get("commitFit") is True
            )
            failed |= verdict(pred_ok, f"closing-predict {theme}", pred)

            browser.ev(
                f"document.open(); document.write({json.dumps(unsealed_html)}); document.close();"
            )
            browser.call("Runtime.evaluate", {"expression": "document.fonts.ready",
                                              "awaitPromise": True})
            unsealed = browser.ev(
                f"{CLOSING_UNSEALED}\nmeasureClosingUnsealed({json.dumps(theme)}, "
                f"{json.dumps(SAMPLE_M10_CMD)})"
            ) or {}
            unsealed_ok = (
                unsealed.get("theme") == theme
                and unsealed.get("doc") == [800, 480]
                and unsealed.get("back") is True
                and unsealed.get("codePx", 0) >= 18
                and unsealed.get("noOverflow") is True
                and unsealed.get("cmdVisibleAtTop") is True
                and unsealed.get("noSplitHyphens") is True
                and unsealed.get("operateFoot") is True
                and unsealed.get("observeFoot") is True
                and unsealed.get("explainFoot") is True
                and unsealed.get("completeVisible") is True
            )
            failed |= verdict(unsealed_ok, f"closing-stages {theme}", unsealed)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
