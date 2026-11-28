# ABOUTME: Check pre-lesson recall cards at 800x480 over Chromium DevTools in all three themes.
# ABOUTME: Seeds a scratch Progress DB before starting its own console; tests pre-answer and Missed.

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

from render_scroll import connect, fence, leave_page  # noqa: E402

from sigint101.app import PACK_DIR, create_app  # noqa: E402
from sigint101.hardware import RadioProbe  # noqa: E402
from sigint101.lessons import load_lessons  # noqa: E402
from sigint101.progress import Progress  # noqa: E402
from sigint101.retrieval import DAY_S  # noqa: E402

THEMES = ("dark", "sunlight", "red-night")

MEASURE_RECALL = """
function measureRecall(expectedTheme, expectedActionLabel) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const panel = document.querySelector('.stage-panel[data-stage="recall"]');
  const title = panel?.querySelector('.stage-panel__title');
  const skip = panel?.querySelector('.recall__skip');
  const primary = panel?.querySelector('.predict__actions .btn--primary');
  const status = panel?.querySelector('[data-choice-status]');
  const whys = [...(panel?.querySelectorAll('.verdict__why') || [])];
  const tag = panel?.querySelector('.verdict__tag');
  const chips = [...(panel?.querySelectorAll('.predict-chip') || [])];
  if (!app || !back || !panel || !title || !skip || !primary || !chips.length) return null;

  function inViewport(r) {
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }
  function fitsTarget(el) {
    const r = el.getBoundingClientRect();
    return inViewport(r) && r.width >= 72 && r.height >= 72 && r.height <= 80;
  }
  function unclipped(el) {
    const r = el.getBoundingClientRect();
    if (!inViewport(r)) return false;
    const cs = getComputedStyle(el);
    if (cs.overflowX !== 'visible' && el.scrollWidth > el.clientWidth + 1) return false;
    if (cs.overflowY !== 'visible' && el.scrollHeight > el.clientHeight + 1) return false;
    return true;
  }

  const promptPx = parseFloat(getComputedStyle(title).fontSize) || 0;
  const statusPx = status ? (parseFloat(getComputedStyle(status).fontSize) || 0) : 18;
  const minWhyPx = whys.length
    ? Math.min(...whys.map(w => parseFloat(getComputedStyle(w).fontSize) || 0))
    : 18;
  const tagStyle = tag ? getComputedStyle(tag) : null;

  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    panelScrolls: panel.scrollHeight > panel.clientHeight + 1,
    back: inViewport(back.getBoundingClientRect()) && back.getBoundingClientRect().height >= 72,
    skipFits: fitsTarget(skip),
    primaryFits: fitsTarget(primary) && primary.textContent.trim() === expectedActionLabel,
    chipsFit: chips.every(unclipped),
    whysFit: whys.every(unclipped),
    promptPx,
    statusPx,
    minWhyPx,
    tagWord: tag ? tag.textContent.trim() : null,
    tagBg: tagStyle ? tagStyle.backgroundColor : null,
    tagShadow: tagStyle ? tagStyle.boxShadow : null,
  };
}
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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


def _post_choice(base_url: str, lesson_id: int, source_id: int, card: int, choice: str) -> str:
    url = f"{base_url}/api/recall?lesson_id={lesson_id}&source={source_id}&card={card}"
    data = urlencode({"choice": choice}).encode("utf-8")
    req = Request(url, data=data, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=5) as resp:
        return resp.geturl()


def _run_server(data_dir: Path, clock_fn):
    port = _free_port()
    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=data_dir,
        clock=clock_fn,
        probe=lambda: RadioProbe(False, "lsusb", "render check"),
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            with urlopen(f"{base}/", timeout=1):
                return server, thread, base
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("scratch recall console did not start")


def _stop_server(server, thread):
    server.should_exit = True
    thread.join(timeout=5)


def verdict(ok, label, measures):
    print(label, json.dumps(measures))
    print(f"VERDICT: {'PASS' if ok else 'FAIL'} - {label}")
    return not ok


def _fits(m, theme) -> bool:
    return (
        m.get("theme") == theme
        and m.get("doc") == [800, 480]
        and not m.get("panelScrolls")
        and bool(m.get("back"))
        and bool(m.get("skipFits"))
        and bool(m.get("primaryFits"))
        and bool(m.get("chipsFit"))
        and bool(m.get("whysFit"))
        and m.get("promptPx", 0) >= 18
        and m.get("statusPx", 0) >= 18
        and m.get("minWhyPx", 0) >= 18
    )


def main(_console=None):
    lessons = load_lessons(PACK_DIR)
    sources = sorted(lid for lid, les in lessons.items() if les.check)
    target = next(lid for lid in sorted(lessons) if lid not in sources)
    t0 = 1_700_000_000.0
    now = [t0 + 2 * DAY_S]

    browser = connect()
    failed = False
    try:
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})

        for theme in THEMES:
            now = [t0 + 2 * DAY_S]
            with tempfile.TemporaryDirectory(prefix="sigint101-recall-render-") as tmp:
                data_dir = Path(tmp)
                # Seed through Progress BEFORE starting the console (one DB handle only).
                prog = Progress(data_dir / "progress.db")
                for idx, lid in enumerate(sources):
                    prog.mark_completed(lid)
                    prog.enroll_recall([lid], now=t0 + idx)
                prog.close()

                server, thread, base = _run_server(data_dir, lambda: now[0])
                try:
                    load_page(browser, f"{base}/")
                    browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
                    miss_tag = None

                    # Walk sources in pairs through fresh target lessons so we measure both
                    # "Next card" (card 1 of 2) and "Start lesson" (card 2 of 2).
                    fresh_targets = [lid for lid in sorted(lessons) if lid not in sources]
                    idx = 0
                    t_idx = 0
                    while idx < len(sources):
                        tgt = fresh_targets[t_idx % len(fresh_targets)]
                        t_idx += 1
                        pair = sources[idx:idx + 2]
                        idx += 2

                        for card_pos, src_id in enumerate(pair, start=1):
                            url = (f"{base}/lesson/{tgt}" if card_pos == 1
                                   else f"{base}/lesson/{tgt}?recall=card&card=2")
                            load_page(browser, url)
                            pre = browser.ev(
                                f"{MEASURE_RECALL}\nmeasureRecall({json.dumps(theme)}, "
                                "'Commit answer')"
                            ) or {}
                            failed |= verdict(
                                _fits(pre, theme), f"recall L{src_id} pre {theme}", pre
                            )

                            src_lesson = lessons[src_id]
                            wrong_key = next(
                                c["key"] for c in src_lesson.check["choices"]
                                if c["key"] != src_lesson.check["answer"]
                            )
                            fb_url = _post_choice(base, tgt, src_id, card_pos, wrong_key)
                            load_page(browser, fb_url)
                            expected_next = (
                                "Next card" if (card_pos == 1 and len(pair) == 2)
                                else "Start lesson"
                            )
                            missed = browser.ev(
                                f"{MEASURE_RECALL}\nmeasureRecall({json.dumps(theme)}, "
                                f"{json.dumps(expected_next)})"
                            ) or {}
                            ok = _fits(missed, theme) and missed.get("tagWord") == "Missed"
                            failed |= verdict(ok, f"recall L{src_id} missed {theme}", missed)
                            if miss_tag is None:
                                miss_tag = (missed.get("tagWord"), missed.get("tagBg"),
                                            missed.get("tagShadow"))

                    # Check Right state and confirm Right vs Missed differ in word and shape.
                    now[0] += 2 * DAY_S
                    first_src = sources[0]
                    right_key = lessons[first_src].check["answer"]
                    fb_right = _post_choice(base, target, first_src, 1, right_key)
                    load_page(browser, fb_right)
                    right = browser.ev(
                        f"{MEASURE_RECALL}\nmeasureRecall({json.dumps(theme)}, "
                        f"{json.dumps('Next card' if len(sources) > 1 else 'Start lesson')})"
                    ) or {}
                    right_tag = (right.get("tagWord"), right.get("tagBg"), right.get("tagShadow"))
                    distinct = (
                        right_tag[0] == "Right"
                        and miss_tag is not None
                        and miss_tag[0] == "Missed"
                        and (theme != "red-night"
                             or (right_tag[1] != miss_tag[1] and right_tag[2] != miss_tag[2]))
                    )
                    failed |= verdict(
                        _fits(right, theme) and distinct,
                        f"recall L{first_src} right-vs-missed {theme}",
                        {"right": right_tag, "missed": miss_tag, "fits": _fits(right, theme)},
                    )
                finally:
                    _stop_server(server, thread)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
