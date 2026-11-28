# ABOUTME: Check M26's faded consolidation pilot at 800x480 over Chromium DevTools in 3 themes.
# ABOUTME: Uses a scratch console so live progress stays untouched across all M26 stages.

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import uvicorn

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from render_scroll import connect  # noqa: E402

from sigint101.app import PACK_DIR, create_app  # noqa: E402
from sigint101.hardware import RadioProbe  # noqa: E402

THEMES = ("dark", "sunlight", "red-night")
LESSON_ID = 26

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
  const strip = panel.querySelector('[data-faded-tune="ais"]');
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

  const head = panel.querySelector('.decoder__head');
  const chromeText = [head, strip, body].map(el => el ? (el.innerText || '') : '').join(' ');
  const fullText = panel.innerText || '';
  const chromeNamesDecoder = /rtl[_\\s-]?ais|aisdecoder/i.test(chromeText);
  const namesDecoder = /rtl[_\\s-]?ais|aisdecoder/i.test(fullText);

  return {
    theme: app.dataset.theme,
    stage: stageName,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    back: inViewport(back.getBoundingClientRect()) && back.getBoundingClientRect().height >= 72,
    badge: badge ? badge.textContent.trim() : '',
    emptyText: empty ? empty.textContent.trim() : '',
    freqText: freq ? freq.textContent.trim() : '',
    namesDecoder,
    chromeNamesDecoder,
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def load_page(browser, url):
    nav = browser.call("Page.navigate", {"url": url})
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


def _post_predict(base_url: str, lesson_id: int, choice: str) -> None:
    url = f"{base_url}/api/predict?lesson_id={lesson_id}"
    data = urlencode({"prediction": choice}).encode("utf-8")
    req = Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(req, timeout=5):
        pass


def _run_server(data_dir: Path):
    port = _free_port()
    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=data_dir,
        probe=lambda: RadioProbe(present=False, known=True, method="lsusb", detail="render check"),
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10.0
    wait = threading.Event()
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{base}/", timeout=1):
                return server, thread, base
        except OSError:
            wait.wait(0.05)
    raise RuntimeError("scratch fading console did not start")


def _stop_server(server, thread):
    server.should_exit = True
    thread.join(timeout=5)


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
        and not m.get("chromeNamesDecoder")
        and bool(m.get("buttonsFit"))
        and not m.get("buttonsOverlap")
        and bool(m.get("dialFits"))
        and m.get("narrationH", 0) >= 58
    )


def _untuned_ok(m, theme) -> bool:
    return (
        _stage_layout_ok(m, theme)
        and not m.get("namesDecoder")
        and m.get("badge") == "UNTUNED"
        and "Not tuned yet" in (m.get("emptyText") or "")
        and (m.get("freqText") or "").startswith("100.0")
    )


def _offband_ok(m, theme) -> bool:
    return (
        _stage_layout_ok(m, theme)
        and not m.get("namesDecoder")
        and m.get("badge") == "OFF BAND"
        and "100.0 MHz" in (m.get("emptyText") or "")
        and "162 MHz" in (m.get("emptyText") or "")
    )


def _tuned_ok(m, theme) -> bool:
    return (
        _stage_layout_ok(m, theme)
        and (m.get("freqText") or "").startswith("162.0")
        and m.get("badge") not in ("", "UNTUNED", "OFF BAND", "LIVE")
    )


def _explain_ok(m, theme) -> bool:
    return (
        m.get("theme") == theme
        and m.get("doc") == [800, 480]
        and bool(m.get("completeFits"))
    )


def main(_console=None):
    browser = connect()
    failed = False
    try:
        browser.call("Page.enable")
        browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False},
        )

        for theme in THEMES:
            with tempfile.TemporaryDirectory(prefix="sigint101-fading-render-") as tmp:
                server, thread, base = _run_server(Path(tmp))
                try:
                    load_page(browser, f"{base}/")
                    browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")

                    # 1) Pre-predict screen: names only the band, hides decoder, fits 800x480.
                    load_page(browser, f"{base}/lesson/{LESSON_ID}")
                    pred = browser.ev(
                        f"{MEASURE_FADING_PREDICT}\nmeasureFadingPredict({json.dumps(theme)})"
                    ) or {}
                    failed |= verdict(_predict_ok(pred, theme), f"fading predict {theme}", pred)

                    # 2) Commit prediction and load operate stage (initially UNTUNED at 100.0 MHz).
                    _post_predict(base, LESSON_ID, "mmsi")
                    load_page(browser, f"{base}/lesson/{LESSON_ID}")
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
                    time.sleep(0.4)
                    tuned = browser.ev(
                        f"{MEASURE_FADING_STAGE}\n"
                        f"measureFadingStage('operate') /* tuned {theme} */"
                    ) or {}
                    failed |= verdict(
                        _tuned_ok(tuned, theme), f"fading operate tuned {theme}", tuned
                    )

                    # 5) Advance to observe (tightest row: Prev + Next step beside tune controls).
                    browser.ev(
                        "document.querySelector('.stage-panel[data-stage=\"operate\"] "
                        "[data-nav=\"next\"]')?.click()"
                    )
                    observe = browser.ev(
                        f"{MEASURE_FADING_STAGE}\n"
                        f"measureFadingStage('observe') /* observe {theme} */"
                    ) or {}
                    failed |= verdict(
                        _stage_layout_ok(observe, theme), f"fading observe {theme}", observe
                    )

                    # 6) Advance to explain and check Complete lesson button fits 800x480.
                    browser.ev(
                        "document.querySelector('.stage-panel[data-stage=\"observe\"] "
                        "[data-nav=\"next\"]')?.click()"
                    )
                    explain = browser.ev(
                        f"{MEASURE_FADING_EXPLAIN}\nmeasureFadingExplain({json.dumps(theme)})"
                    ) or {}
                    failed |= verdict(
                        _explain_ok(explain, theme), f"fading explain {theme}", explain
                    )
                finally:
                    _stop_server(server, thread)
    finally:
        browser.s.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
