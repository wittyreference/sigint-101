# ABOUTME: Check M39's populated Wi-Fi survey panel at 800x480 across all three themes over CDP.
# ABOUTME: Feeds the dense multi-band fixture through survey.py into /lesson/39 and verifies fit.

import json
import shlex
import sys
import tempfile
import time
from pathlib import Path

from render_scroll import connect, leave_page

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sigint101.survey import SURVEY_COMMAND, channel_census, parse_nmcli_wifi  # noqa: E402

THEMES = ("dark", "sunlight", "red-night")
FIXTURE = REPO / "tests" / "fixtures" / "survey" / "dense_nmcli.txt"
EXPECTED_BADGE = "CACHED"


def populated_payload() -> dict:
    """Build the exact /api/survey JSON body from the committed dense multi-band fixture."""
    networks = parse_nmcli_wifi(FIXTURE.read_text(encoding="utf-8"))
    return {
        "available": True,
        "cached": True,
        "command": shlex.join(SURVEY_COMMAND),
        "total": len(networks),
        "channels": channel_census(networks),
        "networks": [net.as_dict() for net in networks],
    }


def fetch_shim(payload: dict) -> str:
    """Stub /api/survey with the fixture and block state-writing POSTs (/api/predict, etc.)."""
    body = json.dumps(payload)
    return f"""
(function () {{
  const origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {{
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = ((init && init.method) || 'GET').toUpperCase();
    if (url.indexOf('/api/survey') !== -1) {{
      return Promise.resolve(new Response({json.dumps(body)}, {{
        status: 200,
        headers: {{ 'content-type': 'application/json' }},
      }}));
    }}
    if (method !== 'GET' ||
        url.indexOf('/api/predict') !== -1 ||
        url.indexOf('/api/stage') !== -1 ||
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


def _render_unsealed_m39_html(shim: str) -> str:
    """Render the real unsealed /lesson/39 HTML in an isolated temp app — zero console writes."""
    from fastapi.testclient import TestClient

    from sigint101.app import create_app
    from sigint101.hardware import RadioProbe

    with tempfile.TemporaryDirectory(prefix="sigint101-survey-html-") as tmp:
        app = create_app(
            pack_dir=REPO / "lesson_packs",
            data_dir=Path(tmp),
            probe=lambda: RadioProbe(False, "lsusb", "render check"),
        )
        with TestClient(app) as client:
            client.post("/api/predict?lesson_id=39", data={"prediction": "envelope-only"},
                        follow_redirects=False)
            html = client.get("/lesson/39").text
    inject = f"<script>{shim}</script>"
    return html.replace("<head>", f"<head>\n{inject}", 1) if "<head>" in html else inject + html


SURVEY = """
function measureSurvey(expectedTheme) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const panels = [...document.querySelectorAll('.stage-panel')];
  const operate = document.querySelector('.stage-panel[data-stage="operate"]');
  const explain = document.querySelector('.stage-panel[data-stage="explain"]');
  if (!app || !back || !operate || !explain) return null;

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

  panels.forEach(p => { p.hidden = (p !== operate); });
  const list = operate.querySelector('[data-survey-list]');
  const badge = operate.querySelector('[data-survey-badge]');
  const items = list ? [...list.querySelectorAll('li')] : [];
  const listRect = list ? list.getBoundingClientRect() : null;
  const buttons = [...operate.querySelectorAll('.tune button')];
  const docW = document.documentElement.scrollWidth;
  const docH = document.documentElement.scrollHeight;
  const listScrolls = !!list && list.scrollHeight > list.clientHeight &&
                      list.scrollWidth <= list.clientWidth + 1;
  const noRowOverflow = !!listRect && items.length > 0 && items.every(li => {
    const r = li.getBoundingClientRect();
    return r.right <= listRect.right + 1 && li.scrollWidth <= li.clientWidth + 1;
  });
  const controlsFit = buttons.length > 0 && buttons.every(
    b => unclipped(b) && b.getBoundingClientRect().height >= 72
  );
  const backRect = back.getBoundingClientRect();
  const backOk = inViewport(backRect) && backRect.height >= 72;

  panels.forEach(p => { p.hidden = (p !== explain); });
  const expBody = explain.querySelector('.stage-panel__body');
  const complete = explain.querySelector('[data-complete]');
  const explainScrolls = !!expBody && expBody.scrollHeight > expBody.clientHeight;
  const completeRect = complete ? complete.getBoundingClientRect() : null;
  const completeVisible = !!completeRect && inViewport(completeRect) && completeRect.height >= 72;

  return {
    theme: app.dataset.theme,
    doc: [docW, docH],
    back: backOk,
    badge: badge ? badge.textContent.trim() : '',
    rows: items.length,
    listScrolls,
    noRowOverflow,
    controlsFit,
    explainScrolls,
    completeVisible,
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
        shim = fetch_shim(populated_payload())
        # connect() has already blocked the whole of /api/* (render_scroll.block_console_api),
        # /api/predict, /api/stage and /api/complete with the rest.
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        browser.call("Page.addScriptToEvaluateOnNewDocument", {"source": shim})
        lesson_url = f"{console}/lesson/39"
        load_page(browser, lesson_url)
        # Never POST /api/predict into the running console (F1): if L39 is sealed on the target,
        # render the unsealed HTML once from an isolated temp app and write it into the page.
        unsealed_html = None
        if browser.ev("!!document.querySelector('.stage-panel[data-stage=\"operate\"]')") is False:
            unsealed_html = _render_unsealed_m39_html(shim)

        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            load_page(browser, lesson_url)
            if unsealed_html is not None:
                browser.ev(
                    f"document.open(); document.write({json.dumps(unsealed_html)}); "
                    "document.close();"
                )
                browser.call("Runtime.evaluate", {"expression": "document.fonts.ready",
                                                  "awaitPromise": True})
            for _ in range(20):
                count = browser.ev("document.querySelectorAll('[data-survey-list] li').length")
                if count is None or count > 0:
                    break
                time.sleep(0.1)
            measures = browser.ev(f"{SURVEY}\nmeasureSurvey({json.dumps(theme)})") or {}
            ok = (
                measures.get("theme") == theme
                and measures.get("doc") == [800, 480]
                and measures.get("back") is True
                and measures.get("badge") == EXPECTED_BADGE
                and measures.get("rows", 0) >= 8
                and measures.get("listScrolls") is True
                and measures.get("noRowOverflow") is True
                and measures.get("controlsFit") is True
                and measures.get("explainScrolls") is True
                and measures.get("completeVisible") is True
            )
            failed |= verdict(ok, f"survey {theme}", measures)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
