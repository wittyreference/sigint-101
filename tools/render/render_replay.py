# ABOUTME: Measures replay controls, rows, and logbook marks at 800x480 in each kiosk theme.
# ABOUTME: Uses captured decoder text and scratch progress while the measured console gets no posts.

import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from render_logbook import THEMES, load_page, verdict
from render_scroll import connect, leave_page

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sigint101.app import create_app  # noqa: E402
from sigint101.decode import parse_decode_line  # noqa: E402
from sigint101.hardware import RadioProbe  # noqa: E402
from sigint101.progress import Progress  # noqa: E402
from sigint101.stamps import mint  # noqa: E402

STEM = "fixture"
DIGEST = "a" * 64
FIXTURE = REPO / "tests" / "fixtures" / "rtl_433" / "acurite-tower-repeats.txt"

STUB = r"""
(rows => {
  const original = window.fetch;
  let started = false, polls = 0;
  const replay = () => ({mode: 'replay', replay: true, state: started ? 'replaying' : 'idle',
    recording: 'fixture', frequency_hz: 433920000, sample_rate: 250000,
    decodes: started ? rows : [],
    command: 'rtl_433 -F json -M level -s 250000 -f 433920000 -r cu8:-',
    input: 'recordings/fixture.sigmf-data'});
  const reply = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), {
    status, headers: {'content-type': 'application/json'}
  }));
  window.fetch = (address, options) => {
    const path = new URL(address, location.href).pathname;
    if (path === '/api/replay/recordings') return reply({recordings: [
      {instrument: 'ism', stem: 'fixture'}], refused: []});
    if (path === '/api/replay/ism/start') { started = true; return reply(replay()); }
    if (path === '/api/replay/ism/stop') { started = false; return reply(replay()); }
    if (path === '/api/replay/ism') return reply(replay());
    if (path === '/api/decode') {
      if (++polls === 1) {
        return reply({available: false, cause: 'no_radio', reason: 'No radio'}, 503);
      }
      return reply({available: true, state: 'running', decodes: rows});
    }
    if (path.startsWith('/api/') || (options?.method && options.method !== 'GET')) {
      return reply({available: false}, 503);
    }
    return original(address, options);
  };
})(%s)
"""

MEASURE_PANEL = r"""
function measureReplay(theme, badgeText, lesson) {
  const within = (el) => {
    const r = el?.getBoundingClientRect();
    return !!r && r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth && r.bottom <= innerHeight;
  };
  const screen = lesson ? document.querySelector('.stage-panel[data-stage="demo"]') : document;
  const panel = screen?.querySelector('[data-decode-panel]');
  const badge = panel?.querySelector('[data-decode-badge]');
  const key = screen?.querySelector('[data-replay-toggle]');
  if (!panel || !badge || !key || badge.textContent !== badgeText || key.disabled) return null;
  const strip = key.closest('.tune');
  const style = getComputedStyle(badge);
  const rows = [...panel.querySelectorAll('.decoder__item')];
  const hidden = [...strip.querySelectorAll('[data-tune-step], [data-step-cycle]')]
               .every(el => getComputedStyle(el).display === 'none');
  const levels = [style.color, getComputedStyle(panel).backgroundColor].map(rgb => {
    return rgb.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => {
      const s = v / 255;
      return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, s, i) => sum + s * [0.2126, 0.7152, 0.0722][i], 0);
  }).sort((a, b) => a - b);
  return {
    theme: document.querySelector('.app').dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    border: style.borderTopStyle, pill: badge.textContent,
    key: [key.getBoundingClientRect().height, parseFloat(getComputedStyle(key).fontSize)],
    controls: !key.closest('.decoder') && within(key) &&
      within(document.querySelector('.btn-back')) &&
      (!lesson || (within(screen.querySelector('[data-nav="next"]')) &&
                   within(screen.querySelector('[data-nav="prev"]')))),
    rows: rows.length,
    marked: rows.every(row => row.querySelector('.logbook-mark--replay')?.textContent === 'REPLAY'),
    rowsFit: rows.every(row => row.scrollWidth <= row.clientWidth + 1),
    hidden, caption: strip.querySelector('[data-replay-caption]').textContent,
    contrast: (levels[1] + 0.05) / (levels[0] + 0.05),
  };
}
"""

MEASURE_BOOK = r"""
(html => {
  const fragment = new DOMParser().parseFromString(html, 'text/html').querySelector('.logbook');
  document.querySelector('.logbook').replaceWith(fragment);
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back[href="/operate"]')?.getBoundingClientRect();
  const list = fragment.querySelector('.logbook__entries');
  const row = list.querySelector('[data-logbook-instrument="ism"]');
  const legend = fragment.querySelector('.logbook__legend');
  const marks = [...row.querySelectorAll('.logbook__marks .logbook-mark')];
  const contrast = marks.map(mark => {
    const style = getComputedStyle(mark);
    const paper = style.backgroundColor === 'rgba(0, 0, 0, 0)'
      ? getComputedStyle(row).backgroundColor : style.backgroundColor;
    const levels = [style.color, paper].map(rgb => {
      return rgb.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => {
        const s = v / 255;
        return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      }).reduce((sum, s, i) => sum + s * [0.2126, 0.7152, 0.0722][i], 0);
    }).sort((a, b) => a - b);
    return (levels[1] + 0.05) / (levels[0] + 0.05);
  });
  list.scrollTop = list.scrollHeight;
  const last = list.querySelector('.logbook__entry:last-child').getBoundingClientRect();
  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    back: !!back && back.height >= 72 && back.top >= 0 && back.bottom <= innerHeight,
    scrolls: list.scrollTop > 0 && last.bottom <= list.getBoundingClientRect().bottom + 1,
    legend: legend.textContent.includes('Seen in a replay of a recording.'),
    marks: marks.map(mark => mark.textContent), contrast,
    fits: row.scrollWidth <= row.clientWidth + 1 &&
          (marks.length !== 2 || marks[0].getBoundingClientRect().right <=
                                marks[1].getBoundingClientRect().left),
  };
})(%s)
"""


def fixture_rows():
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        row = parse_decode_line(line.partition(" ")[2])
        if row and row.as_dict() not in rows:
            rows.append(row.as_dict())
    if len(rows) < 2:
        raise RuntimeError("the replay render fixture needs two captured rows")
    return rows[:2]


def logbook_fixtures(data_dir):
    """Render both ledger states from an isolated app with no listening socket or radio."""
    data_dir = Path(data_dir)
    with_progress = Progress(data_dir / "progress.db")
    try:
        with_progress.record_replay_seen("ism", recording=STEM, digest=DIGEST, ts=123.0)
    finally:
        with_progress.close()
    app = create_app(data_dir=data_dir, probe=lambda: RadioProbe(False, "lsusb", "render check"))
    with TestClient(app) as client:
        replay_only = client.get("/logbook").text
    stamp = mint({"kind": "reception", "ts": 124.0, "payload": {
        "instrument": "ism", "tool": "rtl_433", "command": "rtl_433 -d 0",
        "freq_hz": 433_920_000, "radio": {"kind": "rtl-sdr", "index": 0, "serial": None},
        "item": fixture_rows()[0],
    }})
    progress = Progress(data_dir / "progress.db")
    try:
        progress.record_stamps([stamp])
    finally:
        progress.close()
    with TestClient(app) as client:
        both = client.get("/logbook").text
    return [replay_only, both]


def panel_measure(browser, theme, badge, lesson):
    params = ", ".join(json.dumps(value) for value in (theme, badge, lesson))
    answer = browser.call("Runtime.evaluate", {
        "expression": MEASURE_PANEL + f"""
        (async () => {{
          for (let i = 0; i < 20; i++) {{
            const m = measureReplay({params});
            if (m) return m;
            await new Promise(resolve => setTimeout(resolve, 100));
          }}
          return null;
        }})()
        """, "awaitPromise": True, "returnByValue": True,
    })
    return answer.get("result", {}).get("value") or {}


def _panel_fits(refused, replayed, live, theme, lesson):
    return (
        all(m.get("theme") == theme and m.get("doc") == [800, 480] and m.get("controls")
            and m.get("key", [0, 0])[0] in range(72, 81) and m["key"][1] >= 18
            for m in (refused, replayed, live))
        and refused["border"] == "dashed" and live["border"] == "solid"
        and replayed["border"] == "double" and replayed["contrast"] >= 4.5
        and len({m["pill"] for m in (refused, replayed, live)}) == 3
        and replayed["rows"] == 2 and replayed["marked"] and replayed["rowsFit"]
        and replayed["caption"] == f"REPLAY · {STEM}" and (lesson or replayed["hidden"])
    )


def main(console):
    rows = fixture_rows()
    with tempfile.TemporaryDirectory(prefix="sigint101-replay-render-") as data:
        books = logbook_fixtures(data)
    browser = connect()
    try:
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        browser.call("Page.addScriptToEvaluateOnNewDocument",
                     {"source": STUB % json.dumps(rows)})
        load_page(browser, f"{console}/operate/ism")
        failed = False
        panels = (("M10", "/lesson/10", True), ("rack", "/operate/ism", False))
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            for label, path, lesson in panels:
                load_page(browser, console + path)
                scope = "document"
                if lesson:
                    scope = 'document.querySelector(\'.stage-panel[data-stage="demo"]\')'
                    browser.ev("""document.querySelectorAll('.stage-panel').forEach(
                        panel => panel.hidden = panel.dataset.stage !== 'demo')""")
                refused = panel_measure(browser, theme, "NO RADIO", lesson)
                browser.ev(f'{scope}?.querySelector("[data-replay-toggle]")?.click()')
                replayed = panel_measure(browser, theme, "REPLAY", lesson)
                browser.ev(f'{scope}?.querySelector("[data-replay-toggle]")?.click()')
                live = panel_measure(browser, theme, "LIVE", lesson)
                failed |= verdict(_panel_fits(refused, replayed, live, theme, lesson),
                                  f"replay {label} {theme}", [refused, replayed, live])
            measured = []
            for html in books:
                load_page(browser, f"{console}/logbook")
                measured.append(browser.ev(MEASURE_BOOK % json.dumps(html)) or {})
            ok = all(m.get("theme") == theme and m.get("doc") == [800, 480] and m.get("back")
                     and m.get("scrolls") and m.get("legend") and m.get("fits")
                     and m.get("marks") == marks and min(m["contrast"]) >= 4.5
                     for m, marks in zip(measured, (["REPLAY"], ["STAMP", "REPLAY"]), strict=True))
            failed |= verdict(ok, f"replay logbook {theme}", measured)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
