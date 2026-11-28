# ABOUTME: Check Tier B live-find unit tiles, challenge screens, and rack Back at 800x480 over CDP.
# ABOUTME: Stubs /api/challenges/* across all 4 states and 3 themes without mutating the console.

import json
import sys
import time

from render_scroll import connect, fence, leave_page

RENDER_CHECK = {
    "name": "live-find",
    "pages": ("/unit/4", "/unit/5", "/unit/4/challenge", "/unit/5/challenge",
              "/operate/ism?back=/unit/4/challenge"),
    "scratch": False,
    "order": 90,
}

THEMES = ("dark", "sunlight", "red-night")
STATES = ("idle", "active", "passed", "expired")

STUB_PAYLOADS = {
    4: {
        "idle": {
            "unit_id": 4, "state": "idle", "window_s": 900, "started_at": None,
            "remaining_s": 0, "reason": None, "latest_pass": None, "passes": 0, "error": None,
        },
        "active": {
            "unit_id": 4, "state": "active", "window_s": 900, "started_at": 1764330000.0,
            "remaining_s": 875, "reason": None, "latest_pass": None, "passes": 0, "error": None,
        },
        "passed": {
            "unit_id": 4, "state": "passed", "window_s": 900, "started_at": 1764330000.0,
            "remaining_s": 0, "reason": None, "passes": 1, "error": None,
            "latest_pass": {
                "started_at": 1764330000.0, "passed_at": 1764330131.0,
                "instrument": "ism", "tool": "rtl_433", "freq_hz": 433_920_000,
                "digest": "ab" * 32,
            },
        },
        "expired": {
            "unit_id": 4, "state": "expired", "window_s": 900, "started_at": 1764330000.0,
            "remaining_s": 0, "reason": "clock_moved", "latest_pass": None,
            "passes": 0, "error": None,
        },
    },
    5: {
        "idle": {
            "unit_id": 5, "state": "idle", "window_s": 900, "started_at": None,
            "remaining_s": 0, "reason": None, "latest_pass": None, "passes": 0, "error": None,
        },
        "active": {
            "unit_id": 5, "state": "active", "window_s": 900, "started_at": 1764330000.0,
            "remaining_s": 605, "reason": None, "latest_pass": None, "passes": 0, "error": None,
        },
        "passed": {
            "unit_id": 5, "state": "passed", "window_s": 900, "started_at": 1764330000.0,
            "remaining_s": 0, "reason": None, "passes": 1, "error": None,
            "latest_pass": {
                "started_at": 1764330000.0, "passed_at": 1764330131.0,
                "instrument": "adsb", "tool": "dump1090", "freq_hz": 1_090_000_000,
                "digest": "cd" * 32,
            },
        },
        "expired": {
            "unit_id": 5, "state": "expired", "window_s": 900, "started_at": 1764330000.0,
            "remaining_s": 0, "reason": None, "latest_pass": None, "passes": 0, "error": None,
        },
    },
}


def fetch_shim() -> str:
    """Stub /api/challenges/* before find.js runs so nothing posts or mutates console state."""
    payloads = json.dumps(STUB_PAYLOADS)
    return f"""
(function () {{
  const stubs = {payloads};
  window.__LIVE_FIND_STATE__ = 'idle';
  const origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {{
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const m = url.match(/\\/api\\/challenges\\/(\\d+)(?:\\/start)?/);
    if (m) {{
      const unit = Number(m[1]);
      const state = window.__LIVE_FIND_STATE__ || 'idle';
      const body = (stubs[unit] && stubs[unit][state]) || stubs[4].idle;
      return Promise.resolve(new Response(JSON.stringify(body), {{
        status: 200,
        headers: {{ 'Content-Type': 'application/json' }},
      }}));
    }}
    return origFetch(input, init);
  }};
}})();
"""


TILE_MEASURE = r"""
function measureFindTile(expectedTheme, unitId) {
  const app = document.querySelector('.app');
  const tiles = document.querySelector('.tiles');
  const findTile = document.querySelector('[data-find-tile]');
  const mark = findTile && findTile.querySelector('[data-find-mark]');
  const clk = findTile && findTile.querySelector('[data-find-clock]');
  if (!app || !tiles || !findTile || !mark || !clk) return null;

  const states = ['idle', 'active', 'passed', 'expired'];
  const marks = window.LiveFind;
  if (!marks) return null;

  const seenText = new Set();
  const seenShape = new Set();
  let clipped = 0;
  let maxDocW = document.documentElement.scrollWidth;
  let maxDocH = document.documentElement.scrollHeight;

  for (const st of states) {
    const m = marks.markFor(st);
    mark.textContent = m.word;
    mark.className = m.cls;
    clk.hidden = (st !== 'active');
    clk.textContent = st === 'active' ? marks.clock(875) : '00:00';

    const cs = getComputedStyle(mark);
    seenText.add(mark.textContent.trim());
    seenShape.add([cs.borderTopStyle, cs.borderTopWidth, cs.backgroundColor].join('|'));

    maxDocW = Math.max(maxDocW, document.documentElement.scrollWidth);
    maxDocH = Math.max(maxDocH, document.documentElement.scrollHeight);

    const items = [...document.querySelectorAll('.tile')];
    for (const t of items) {
      const meta = t.querySelector('.tile__meta');
      if (!meta) continue;
      if (meta.getBoundingClientRect().bottom - t.getBoundingClientRect().bottom > 1) {
        clipped += 1;
      }
    }
  }

  return {
    theme: app.dataset.theme,
    doc: [maxDocW, maxDocH],
    overflowY: getComputedStyle(tiles).overflowY,
    tilesWithClippedMeta: clipped,
    distinctMarks: seenText.size === 4 && seenShape.size === 4,
  };
}
"""

SCREEN_MEASURE = r"""
function measureFindScreen(expectedTheme, unitId) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const screen = document.querySelector('[data-find-screen]');
  const prompt = screen && screen.querySelector('[data-find-prompt]');
  const mark = screen && screen.querySelector('[data-find-mark]');
  const clk = screen && screen.querySelector('[data-find-clock]');
  const startBtn = screen && screen.querySelector('[data-find-start]');
  const evidence = screen && screen.querySelector('[data-find-evidence]');
  const links = screen ? [...screen.querySelectorAll('.find-screen__link')] : [];
  if (!app || !back || !screen || !prompt || !mark || !clk || !startBtn ||
      !evidence || !links.length) {
    return null;
  }

  const marks = window.LiveFind;
  if (!marks) return null;

  function inViewport(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }

  const samples = {
    idle: { state: 'idle', remaining_s: 0, reason: null, latest_pass: null, error: null },
    active: { state: 'active', remaining_s: 875, reason: null, latest_pass: null, error: null },
    passed: {
      state: 'passed', remaining_s: 0, reason: null, error: null,
      latest_pass: {
        started_at: 1764330000, passed_at: 1764330131,
        instrument: unitId === 5 ? 'adsb' : 'ism',
        tool: unitId === 5 ? 'dump1090' : 'rtl_433',
        freq_hz: unitId === 5 ? 1090000000 : 433920000,
      },
    },
    expired: {
      state: 'expired', remaining_s: 0, reason: 'clock_moved', latest_pass: null, error: null,
    },
  };

  const seenText = new Set();
  const seenShape = new Set();
  let controlsFit = true;
  let maxDocW = document.documentElement.scrollWidth;
  let maxDocH = document.documentElement.scrollHeight;
  let keyHeights = [];

  for (const st of ['idle', 'active', 'passed', 'expired']) {
    const body = samples[st];
    const m = marks.markFor(st);
    mark.textContent = m.word;
    mark.className = m.cls;
    clk.textContent = marks.clock(body.remaining_s);
    startBtn.textContent = marks.startLabel(st);
    evidence.textContent = marks.evidenceLine(body);

    const cs = getComputedStyle(mark);
    seenText.add(mark.textContent.trim());
    seenShape.add([cs.borderTopStyle, cs.borderTopWidth, cs.backgroundColor].join('|'));

    maxDocW = Math.max(maxDocW, document.documentElement.scrollWidth);
    maxDocH = Math.max(maxDocH, document.documentElement.scrollHeight);

    const allControls = [prompt, mark, clk, startBtn, ...links, evidence];
    if (!allControls.every(inViewport)) controlsFit = false;
    keyHeights = [startBtn, ...links].map(el => Math.round(el.getBoundingClientRect().height));
  }

  const backRect = back.getBoundingClientRect();
  return {
    theme: app.dataset.theme,
    doc: [maxDocW, maxDocH],
    back: inViewport(back) && backRect.height >= 72,
    controlsFit,
    keyHeights,
    promptFontPx: Math.round(parseFloat(getComputedStyle(prompt).fontSize) || 0),
    evidenceFontPx: Math.round(parseFloat(getComputedStyle(evidence).fontSize) || 0),
    distinctMarks: seenText.size === 4 && seenShape.size === 4,
  };
}
"""

RACK_MEASURE = r"""
function measureFindRack(expectedTheme) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const title = document.querySelector('.statusbar__title');
  if (!app || !back || !title) return null;
  const br = back.getBoundingClientRect();
  const tr = title.getBoundingClientRect();
  const backOk = br.width > 0 && br.height >= 72 && br.left >= 0 && br.top >= 0 &&
                 br.right <= innerWidth + 0.5 && br.bottom <= innerHeight + 0.5;
  const titleUnclipped = tr.width > 0 && tr.height > 0 &&
                         title.scrollWidth <= title.clientWidth + 1;
  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    back: backOk,
    backHref: back.getAttribute('href'),
    backLabel: (back.querySelector('.btn-back__label') || back).textContent.trim(),
    titleUnclipped,
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
        # /api/challenges/*/start with the rest; the fetch shim answers the challenge reads.
        browser.call("Page.enable")
        browser.call("Page.addScriptToEvaluateOnNewDocument", {"source": fetch_shim()})
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        load_page(browser, f"{console}/unit/4")
        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            for unit_id in (4, 5):
                load_page(browser, f"{console}/unit/{unit_id}")
                tile = browser.ev(
                    f"{TILE_MEASURE}\nmeasureFindTile({json.dumps(theme)}, {unit_id})"
                ) or {}
                tile_ok = (
                    tile.get("theme") == theme
                    and tile.get("doc") == [800, 480]
                    and tile.get("overflowY") in ("auto", "scroll")
                    and tile.get("tilesWithClippedMeta") == 0
                    and tile.get("distinctMarks") is True
                )
                failed |= verdict(tile_ok, f"live-find tile u{unit_id} {theme}", tile)

                load_page(browser, f"{console}/unit/{unit_id}/challenge")
                screen = browser.ev(
                    f"{SCREEN_MEASURE}\nmeasureFindScreen({json.dumps(theme)}, {unit_id})"
                ) or {}
                heights = screen.get("keyHeights") or []
                screen_ok = (
                    screen.get("theme") == theme
                    and screen.get("doc") == [800, 480]
                    and screen.get("back") is True
                    and screen.get("controlsFit") is True
                    and bool(heights)
                    and all(72 <= h <= 80 for h in heights)
                    and screen.get("promptFontPx", 0) >= 18
                    and screen.get("evidenceFontPx", 0) >= 18
                    and screen.get("distinctMarks") is True
                )
                failed |= verdict(screen_ok, f"live-find screen u{unit_id} {theme}", screen)

            load_page(browser, f"{console}/operate/ism?back=/unit/4/challenge")
            rack = browser.ev(f"{RACK_MEASURE}\nmeasureFindRack({json.dumps(theme)})") or {}
            rack_ok = (
                rack.get("theme") == theme
                and rack.get("doc") == [800, 480]
                and rack.get("back") is True
                and rack.get("backHref") == "/unit/4/challenge"
                and rack.get("backLabel") == "Live find"
                and rack.get("titleUnclipped") is True
            )
            failed |= verdict(rack_ok, f"live-find rack {theme}", rack)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
