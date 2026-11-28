# ABOUTME: Check Tier C solo unit tiles, Hunt/Step A/Step B/Result views, and rack Back at 800x480.
# ABOUTME: Runs an isolated no-radio scratch console seeded with a longest-content test solo.

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import uvicorn

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from render_scroll import connect, fence, leave_page  # noqa: E402

from sigint101.app import PACK_DIR, create_app  # noqa: E402
from sigint101.hardware import RadioProbe  # noqa: E402
from sigint101.solos import Solo, SoloAuditRecord, SoloBandLeg, SoloWhyOption  # noqa: E402

RENDER_CHECK = {
    "name": "solo",
    "pages": ("/unit/4", "/operate/ism?back=/unit/4/solo"),
    "scratch": True,
    "order": 130,
}

THEMES = ("dark", "sunlight", "red-night")


def _render_solo_fixture():
    """Longest-content audited solo hitting prompt, 130-char why chip, and 3-line feedback caps."""
    leg = SoloBandLeg(
        slot=0,
        unit_id=4,
        band_label="433 MHz unlicensed short-range telemetry (FCC Part 15)",
        clue=(
            "Find a nearby unencrypted weather or utility burst on the 433 MHz "
            "short-range band using the rack."
        ),
        hardware_note="",
        instrument="ism",
        band_hz=((433_050_000, 434_790_000),),
        legal_prompt=(
            "You decoded an unencrypted outdoor sensor burst in the US. "
            "May you listen to it under federal law?"
        ),
        legal_answers=("legal",),
        ambiguous=False,
        why_options=(
            SoloWhyOption(
                key="readily-accessible",
                label=(
                    "Unencrypted radio emissions readily accessible to the general public "
                    "are explicitly exempt from the federal wiretap ban."
                ),
                supports=("legal",),
                feedback=(
                    "Right. Unencrypted Part 15 sensor telemetry is readily accessible to the "
                    "public under 18 USC 2511(2)(g)(i), so passive reception is lawful."
                ),
                cite="18 USC 2511(2)(g)(i)",
                source_url="https://www.govinfo.gov/app/details/USCODE-2023-title18/USCODE-2023-title18-partI-chap119-sec2511",
                read_date="2026-11-28",
                law_section="wiretap-open",
            ),
            SoloWhyOption(
                key="all-wireless-private",
                label=(
                    "Every residential wireless sensor transmission is protected as private "
                    "electronic communication even without encryption."
                ),
                supports=("illegal",),
                feedback=(
                    "Unencrypted short-range Part 15 bursts are not treated as private encrypted "
                    "traffic; 18 USC 2511(2)(g)(i) exempts readily accessible radio."
                ),
                cite="18 USC 2511(2)(g)(i)",
                source_url="https://www.govinfo.gov/app/details/USCODE-2023-title18/USCODE-2023-title18-partI-chap119-sec2511",
                read_date="2026-11-28",
                law_section="wiretap-open",
            ),
            SoloWhyOption(
                key="needs-amateur-ticket",
                label=(
                    "Passive reception on 433 MHz requires an amateur radio operator license "
                    "before you may decode any digital telemetry."
                ),
                supports=(),
                feedback=(
                    "Licensing governs transmission, not passive reception of unencrypted Part 15 "
                    "signals readily accessible to the public under federal law."
                ),
                cite="18 USC 2511(2)(g)(i)",
                source_url="https://www.govinfo.gov/app/details/USCODE-2023-title18/USCODE-2023-title18-partI-chap119-sec2511",
                read_date="2026-11-28",
                law_section="wiretap-open",
            ),
        ),
        sources=("18 USC 2511(2)(g)(i)",),
        audit_id="render-audit-u4-0",
        item_revision="render-rev-1",
    )
    solo = Solo(
        id="u4-render-solo",
        unit_id=4,
        title="Unlicensed telemetry solo",
        legs=(leg,),
        window_s=900,
    )
    audits = {
        "render-audit-u4-0": SoloAuditRecord(
            audit_id="render-audit-u4-0",
            solo_id="u4-render-solo",
            slot=0,
            author="render-author",
            evaluator="render-evaluator",
            verdict="pass",
            date="2026-11-28",
            item_revision="render-rev-1",
        )
    }
    return (solo,), audits


TILE_MEASURE = r"""
function measureSoloTile(expectedTheme) {
  const app = document.querySelector('.app');
  const tiles = document.querySelector('.tiles');
  const soloTile = document.querySelector('[data-solo-tile]');
  const mark = soloTile && soloTile.querySelector('[data-solo-mark]');
  const clk = soloTile && soloTile.querySelector('[data-solo-clock]');
  const ui = window.SoloUI;
  if (!app || !tiles || !soloTile || !mark || !clk || !ui) return null;

  const states = ['idle', 'active', 'reception_passed', 'passed', 'call_missed', 'expired'];
  const seenText = new Set();
  const seenShape = new Set();
  let clipped = 0;
  let maxDocW = document.documentElement.scrollWidth;
  let maxDocH = document.documentElement.scrollHeight;

  for (const st of states) {
    const m = ui.markFor(st);
    mark.textContent = m.word;
    mark.className = m.cls;
    clk.hidden = (st !== 'active');
    clk.textContent = st === 'active' ? ui.clock(875) : '00:00';

    const cs = getComputedStyle(mark);
    seenText.add(mark.textContent.trim());
    seenShape.add([cs.borderTopStyle, cs.borderTopWidth, cs.backgroundColor].join('|'));

    maxDocW = Math.max(maxDocW, document.documentElement.scrollWidth);
    maxDocH = Math.max(maxDocH, document.documentElement.scrollHeight);

    for (const t of document.querySelectorAll('.tile')) {
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
    distinctMarks: seenText.size === 6 && seenShape.size === 6,
  };
}
"""


SCREEN_MEASURE = r"""
function measureSoloScreen(expectedTheme, payloads) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const screen = document.querySelector('[data-solo-screen]');
  const apply = window.__soloRenderApply;
  const ui = window.SoloUI;
  if (!app || !back || !screen || !apply || !ui) return null;

  function inViewport(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }
  function unclipped(el) {
    if (!inViewport(el)) return false;
    const cs = getComputedStyle(el);
    if (cs.overflowY !== 'visible' && el.scrollHeight > el.clientHeight + 1) return false;
    return true;
  }
  function fontPx(el) {
    return Math.round(parseFloat(getComputedStyle(el).fontSize) || 0);
  }
  function hasGlow(el) {
    const sh = getComputedStyle(el).textShadow || 'none';
    return sh !== 'none';
  }

  let maxDocW = document.documentElement.scrollWidth;
  let maxDocH = document.documentElement.scrollHeight;
  function trackDoc() {
    maxDocW = Math.max(maxDocW, document.documentElement.scrollWidth);
    maxDocH = Math.max(maxDocH, document.documentElement.scrollHeight);
  }

  // 1. Hunt states (idle, active, expired)
  const huntView = screen.querySelector('[data-solo-view="hunt"]');
  const bandEl = screen.querySelector('[data-solo-band]');
  const clueEl = screen.querySelector('[data-solo-clue]');
  const markEl = screen.querySelector('[data-solo-mark]');
  const clkEl = screen.querySelector('[data-solo-clock]');
  const startBtn = screen.querySelector('[data-solo-start]');
  const rackBtn = screen.querySelector('[data-solo-rack]');
  const evEl = screen.querySelector('[data-solo-evidence]');
  let huntFits = true;
  let huntKeyHeights = [];
  for (const st of ['idle', 'active', 'expired']) {
    apply(payloads[st]);
    trackDoc();
    if (huntView.hidden || huntView.scrollHeight > huntView.clientHeight + 1) huntFits = false;
    if (![bandEl, clueEl, markEl, clkEl, startBtn, rackBtn, evEl].every(inViewport)) {
      huntFits = false;
    }
    huntKeyHeights = [startBtn, rackBtn].map(e => Math.round(e.getBoundingClientRect().height));
  }
  const huntGlow = hasGlow(bandEl) || hasGlow(clueEl);

  // 2. Step A
  apply(payloads.reception_passed, { step: 'a' });
  trackDoc();
  const viewA = screen.querySelector('[data-solo-view="call-a"]');
  const heardEl = screen.querySelector('[data-solo-heard]');
  const promptEl = screen.querySelector('[data-solo-legal-prompt]');
  const choiceBtns = [...screen.querySelectorAll('[data-solo-call-choice]')];
  const stepAFits = (
    !viewA.hidden &&
    viewA.scrollHeight <= viewA.clientHeight + 1 &&
    inViewport(heardEl) && unclipped(promptEl) &&
    choiceBtns.length === 3 && choiceBtns.every(inViewport)
  );
  const stepAHeights = choiceBtns.map(e => Math.round(e.getBoundingClientRect().height));
  const stepAGlow = hasGlow(promptEl);

  // 3. Step B (longest 3 why chips + Change call beside Submit call in one 74 px foot row)
  apply(payloads.reception_passed, { step: 'b', call: 'legal', whyKey: 'readily-accessible' });
  trackDoc();
  const viewB = screen.querySelector('[data-solo-view="call-b"]');
  const pickedEl = screen.querySelector('[data-solo-picked-call]');
  const whyChips = [...screen.querySelectorAll('.solo-why-chip')];
  const changeBtn = screen.querySelector('[data-solo-change-call]');
  const submitBtn = screen.querySelector('[data-solo-submit]');
  const cr = changeBtn.getBoundingClientRect();
  const sr = submitBtn.getBoundingClientRect();
  const footSameRow = Math.abs(cr.top - sr.top) <= 2 && cr.right < sr.left;
  const stepBFits = (
    !viewB.hidden &&
    viewB.scrollHeight <= viewB.clientHeight + 1 &&
    inViewport(pickedEl) &&
    whyChips.length === 3 && whyChips.every(unclipped) &&
    inViewport(changeBtn) && inViewport(submitBtn) &&
    footSameRow
  );
  const stepBChipHeights = whyChips.map(e => Math.round(e.getBoundingClientRect().height));
  const stepBFootHeights = [Math.round(cr.height), Math.round(sr.height)];
  const stepBBottom = Math.round(Math.max(cr.bottom, sr.bottom));

  // 4. Result (PASSED and MISSED)
  const viewRes = screen.querySelector('[data-solo-view="result"]');
  const resMark = screen.querySelector('[data-solo-result-mark]');
  const resHeard = screen.querySelector('[data-solo-result-heard]');
  const partRx = screen.querySelector('[data-solo-part-rx]');
  const partLaw = screen.querySelector('[data-solo-part-law]');
  const fbEl = screen.querySelector('[data-solo-feedback]');
  const citeEl = screen.querySelector('[data-solo-cite]');
  const retryBtn = screen.querySelector('[data-solo-retry]');
  const lawBtn = screen.querySelector('[data-solo-law]');
  let resultFits = true;
  let resultHeights = [];
  const partShapes = new Set();
  for (const st of ['passed', 'call_missed']) {
    apply(payloads[st]);
    trackDoc();
    if (viewRes.hidden || viewRes.scrollHeight > viewRes.clientHeight + 1) resultFits = false;
    if (![resMark, resHeard, partRx, partLaw, retryBtn, lawBtn].every(inViewport) ||
        !unclipped(fbEl) || !unclipped(citeEl)) {
      resultFits = false;
    }
    const csLaw = getComputedStyle(partLaw);
    partShapes.add(
      [partLaw.textContent.trim(), csLaw.borderTopStyle, csLaw.backgroundColor].join('|')
    );
    resultHeights = [retryBtn, lawBtn].map(e => Math.round(e.getBoundingClientRect().height));
  }

  const backRect = back.getBoundingClientRect();
  return {
    theme: app.dataset.theme,
    doc: [maxDocW, maxDocH],
    back: inViewport(back) && backRect.height >= 72,
    huntFits,
    huntKeyHeights,
    stepAFits,
    stepAHeights,
    stepBFits,
    footSameRow,
    stepBChipHeights,
    stepBFootHeights,
    stepBBottom,
    resultFits,
    resultHeights,
    distinctResultParts: partShapes.size === 2,
    promptGlow: huntGlow || stepAGlow,
    minFontPx: Math.min(
      fontPx(bandEl), fontPx(clueEl), fontPx(evEl), fontPx(heardEl), fontPx(promptEl),
      fontPx(pickedEl), ...whyChips.map(fontPx), fontPx(fbEl), fontPx(citeEl),
    ),
  };
}
"""


RACK_MEASURE = r"""
function measureSoloRack(expectedTheme) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const title = document.querySelector('.statusbar__title');
  const rdsPick = document.querySelector('[data-rack-pick="rds"]');
  if (!app || !back || !title || !rdsPick) return null;
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
    rdsPickHref: rdsPick.getAttribute('href'),
    titleUnclipped,
  };
}
"""


def _screen_payloads(solo: Solo) -> dict:
    leg = solo.legs[0]
    base_leg = {
        "slot": leg.slot,
        "band_label": leg.band_label,
        "clue": leg.clue,
        "hardware_note": leg.hardware_note,
        "passed": False,
        "called": False,
    }
    heard_leg = {
        **base_leg,
        "passed": True,
        "evidence": "ISM · rtl_433 · 433.92 MHz · 2026-11-28 12:02:11 UTC",
        "legal_prompt": leg.legal_prompt,
        "why_options": [{"key": o.key, "label": o.label} for o in leg.why_options],
    }
    opt_ok = leg.why_options[0]
    opt_miss = leg.why_options[2]
    return {
        "idle": {
            "id": solo.id, "unit_id": solo.unit_id, "title": solo.title,
            "state": "idle", "window_s": 900, "started_at": None, "remaining_s": 0,
            "reason": None, "error": None, "passed_before_date": "2026-11-28",
            "legs": [base_leg],
        },
        "active": {
            "id": solo.id, "unit_id": solo.unit_id, "title": solo.title,
            "state": "active", "window_s": 900, "started_at": 1_764_330_000.0,
            "remaining_s": 875, "reason": None, "error": None,
            "passed_before_date": "2026-11-28", "legs": [base_leg],
        },
        "expired": {
            "id": solo.id, "unit_id": solo.unit_id, "title": solo.title,
            "state": "expired", "window_s": 900, "started_at": 1_764_330_000.0,
            "remaining_s": 0, "reason": "clock_moved", "error": None,
            "passed_before_date": None, "legs": [base_leg],
        },
        "reception_passed": {
            "id": solo.id, "unit_id": solo.unit_id, "title": solo.title,
            "state": "reception_passed", "window_s": 900, "started_at": 1_764_330_000.0,
            "remaining_s": 0, "reason": None, "error": None,
            "passed_before_date": None, "legs": [heard_leg],
        },
        "passed": {
            "id": solo.id, "unit_id": solo.unit_id, "title": solo.title,
            "state": "passed", "window_s": 900, "started_at": 1_764_330_000.0,
            "remaining_s": 0, "reason": None, "error": None,
            "passed_before_date": "2026-11-28",
            "legs": [{
                **heard_leg, "called": True,
                "call_result": {
                    "call": "legal", "why_key": opt_ok.key,
                    "call_correct": True, "why_correct": True, "legal_passed": True,
                    "feedback": opt_ok.feedback, "cite": opt_ok.cite,
                    "law_section": opt_ok.law_section, "item_revision": leg.item_revision,
                },
            }],
        },
        "call_missed": {
            "id": solo.id, "unit_id": solo.unit_id, "title": solo.title,
            "state": "call_missed", "window_s": 900, "started_at": 1_764_330_000.0,
            "remaining_s": 0, "reason": None, "error": None,
            "passed_before_date": None,
            "legs": [{
                **heard_leg, "called": True,
                "call_result": {
                    "call": "legal", "why_key": opt_miss.key,
                    "call_correct": True, "why_correct": False, "legal_passed": False,
                    "feedback": opt_miss.feedback, "cite": opt_miss.cite,
                    "law_section": opt_miss.law_section, "item_revision": leg.item_revision,
                },
            }],
        },
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_server(data_dir: Path, clock_fn):
    solos, audits = _render_solo_fixture()
    port = _free_port()
    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=data_dir,
        clock=clock_fn,
        probe=lambda: RadioProbe(False, "lsusb", "render check"),
        solos=solos,
        solo_audits=audits,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            with urlopen(f"{base}/", timeout=1):
                return server, thread, base, solos[0]
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("scratch solo console did not start")


def _stop_server(server, thread):
    server.should_exit = True
    thread.join(timeout=5)


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


def _heights_ok(heights, lo=72, hi=80) -> bool:
    return bool(heights) and all(lo <= h <= hi for h in heights)


def main(_console=None):
    browser = connect()
    failed = False
    try:
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        with tempfile.TemporaryDirectory(prefix="sigint101-solo-render-") as tmp:
            server, thread, base, solo = _run_server(Path(tmp), lambda: 1_764_330_000.0)
            payloads_json = json.dumps(_screen_payloads(solo))
            try:
                load_page(browser, f"{base}/unit/4")
                for theme in THEMES:
                    browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")

                    load_page(browser, f"{base}/unit/4")
                    tile = browser.ev(f"{TILE_MEASURE}\nmeasureSoloTile({json.dumps(theme)})") or {}
                    tile_ok = (
                        tile.get("theme") == theme
                        and tile.get("doc") == [800, 480]
                        and tile.get("overflowY") in ("auto", "scroll")
                        and tile.get("tilesWithClippedMeta") == 0
                        and tile.get("distinctMarks") is True
                    )
                    failed |= verdict(tile_ok, f"solo tile u4 {theme}", tile)

                    load_page(browser, f"{base}/unit/4/solo")
                    screen = browser.ev(
                        f"{SCREEN_MEASURE}\nmeasureSoloScreen({json.dumps(theme)}, {payloads_json})"
                    ) or {}
                    screen_ok = (
                        screen.get("theme") == theme
                        and screen.get("doc") == [800, 480]
                        and screen.get("back") is True
                        and screen.get("huntFits") is True
                        and _heights_ok(screen.get("huntKeyHeights"))
                        and screen.get("stepAFits") is True
                        and _heights_ok(screen.get("stepAHeights"))
                        and screen.get("stepBFits") is True
                        and screen.get("footSameRow") is True
                        and _heights_ok(screen.get("stepBChipHeights"), lo=72, hi=84)
                        and _heights_ok(screen.get("stepBFootHeights"))
                        and screen.get("resultFits") is True
                        and _heights_ok(screen.get("resultHeights"))
                        and screen.get("distinctResultParts") is True
                        and screen.get("promptGlow") is False
                        and screen.get("minFontPx", 0) >= 18
                    )
                    failed |= verdict(screen_ok, f"solo screen u4 {theme}", screen)

                    load_page(browser, f"{base}/operate/ism?back=/unit/4/solo")
                    rack = browser.ev(
                        f"{RACK_MEASURE}\nmeasureSoloRack({json.dumps(theme)})"
                    ) or {}
                    rack_ok = (
                        rack.get("theme") == theme
                        and rack.get("doc") == [800, 480]
                        and rack.get("back") is True
                        and rack.get("backHref") == "/unit/4/solo"
                        and rack.get("backLabel") == "Solo"
                        and rack.get("rdsPickHref") == "/operate/rds?back=/unit/4/solo"
                        and rack.get("titleUnclipped") is True
                    )
                    failed |= verdict(rack_ok, f"solo rack {theme}", rack)
            finally:
                _stop_server(server, thread)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
