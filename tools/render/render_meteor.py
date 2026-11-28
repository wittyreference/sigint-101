# ABOUTME: Check the Meteor-M LRPT rack view at 800x480 over Chromium DevTools in all three themes.
# ABOUTME: Exercises every live phase, refusal wrap, and populated gallery with in-page stubs only.

import json
import sys
import time

from render_scroll import connect, fence, leave_page

THEMES = ("dark", "sunlight", "red-night")

# 4x4 synthetic PNG (pure in-page data URI; never touches /media or any console API).
SAMPLE_PNG_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAFElEQVR4nGP8z8DAwMDAxEA1AAEA"
    "BgkBAf0a5c8AAAAASUVORK5CYII="
)

LONG_CMD = (
    "satdump live meteor_m2-x_lrpt /var/lib/sigint101/data/meteor/work/pass "
    "--source rtlsdr --frequency 137900000 --finish_processing"
)

METEOR_CHECK = """
async function measureMeteor(expectedTheme, sampleUri, longCmd) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const panel = document.querySelector('[data-meteor-panel]');
  const btn = panel?.querySelector('[data-meteor-listen]');
  const phaseEl = panel?.querySelector('[data-meteor-phase]');
  const noteEl = panel?.querySelector('[data-meteor-note]');
  const gallery = panel?.querySelector('[data-meteor-gallery]');
  if (!app || !back || !panel || !btn || !phaseEl || !noteEl || !gallery) return null;
  if (typeof window.__meteorRefresh !== 'function') return null;

  function inViewport(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth && r.bottom <= innerHeight;
  }

  const samplePass = {
    id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    started_at: 1732795200,
    ended_at: 1732795500,
    freq_hz: 137900000,
    mode: '72k',
    command: longCmd,
    radio: { kind: 'rtl-sdr', serial: 'rx01', index: 0 },
    satellite: 'METEOR-M2 3',
    result: 'done',
    lock_ever: true,
    ended_by: 'handoff',
    images: [{ path: sampleUri, product: 'rgb' }],
  };

  const cases = [
    { name: 'idle', ok: true, body: {
      available: true, state: 'idle', freq_hz: 137900000, mode: '72k', lock: 'unknown',
      reason: 'Ready to listen', command: null, passes: [],
    }},
    { name: 'searching', ok: true, body: {
      available: true, state: 'searching', freq_hz: 137900000, mode: '72k', lock: 'searching',
      reason: 'Listening on 137.900 MHz · searching', command: longCmd, passes: [],
    }},
    { name: 'receiving', ok: true, body: {
      available: true, state: 'receiving', freq_hz: 137100000, mode: '80k', lock: 'locked',
      reason: 'Listening on 137.100 MHz · locked', command: longCmd, passes: [],
    }},
    { name: 'decoding', ok: true, body: {
      available: true, state: 'decoding', freq_hz: 137900000, mode: '72k', lock: 'locked',
      elapsed_s: 74.2, command: longCmd, passes: [],
    }},
    { name: 'interrupted', ok: true, body: {
      available: true, state: 'interrupted', freq_hz: 137900000, mode: '72k', lock: 'unknown',
      ended_by: 'watchdog', reason: 'SatDump exceeded its finishing deadline.',
      command: null, passes: [],
    }},
    { name: 'no_picture', ok: true, body: {
      available: true, state: 'no_picture', freq_hz: 137900000, mode: '72k', lock: 'unknown',
      reason: 'No picture was decoded from the pass.', command: null, passes: [],
    }},
    { name: 'refusal', ok: false, body: {
      available: false, cause: 'no_radio',
      reason: 'No RTL-SDR found on the USB bus.',
      message: 'No RTL-SDR found on the USB bus.',
      next_step: 'Plug in the dongle and tap Listen now.',
      command: null, passes: [samplePass],
    }},
    { name: 'gallery', ok: true, body: {
      available: true, state: 'done', freq_hz: 137900000, mode: '72k', lock: 'locked',
      reason: 'Pass saved', command: null, passes: [samplePass],
    }},
  ];

  const origFetch = window.fetch;
  let current = cases[0];
  let allDocFit = true;
  let allBackFit = true;
  let allListenFit = true;
  let listenOutsideGallery = true;
  let noHorizOverflow = true;
  let unclippedText = true;
  let maxPhaseLen = 0;
  let maxNoteLen = 0;
  let minFontPx = 999;
  let minListenH = 999;
  let minBackH = 999;
  let imageLoaded = false;
  let detailsVisible = false;
  let redNightFiltered = expectedTheme !== 'red-night';

  window.fetch = () => Promise.resolve({
    ok: current.ok,
    status: current.ok ? 200 : 503,
    json: () => Promise.resolve(current.body),
  });

  try {
    for (const c of cases) {
      current = c;
      await window.__meteorRefresh();
      const docW = document.documentElement.scrollWidth;
      const docH = document.documentElement.scrollHeight;
      if (docW !== 800 || docH !== 480) allDocFit = false;

      const backR = back.getBoundingClientRect();
      minBackH = Math.min(minBackH, backR.height);
      if (!inViewport(back) || backR.height < 72) allBackFit = false;

      const btnR = btn.getBoundingClientRect();
      minListenH = Math.min(minListenH, btnR.height);
      if (!inViewport(btn) || btnR.height < 72) allListenFit = false;
      if (gallery.contains(btn)) listenOutsideGallery = false;

      maxPhaseLen = Math.max(maxPhaseLen, (phaseEl.textContent || '').length);
      if (!noteEl.hidden) {
        maxNoteLen = Math.max(maxNoteLen, (noteEl.textContent || '').length);
      }

      for (const el of [phaseEl, noteEl]) {
        if (el.hidden) continue;
        minFontPx = Math.min(minFontPx, parseFloat(getComputedStyle(el).fontSize));
        if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) {
          unclippedText = false;
        }
      }
      if (panel.scrollWidth > panel.clientWidth + 1 ||
          gallery.scrollWidth > gallery.clientWidth + 1) {
        noHorizOverflow = false;
      }

      if (c.name === 'gallery') {
        const img = gallery.querySelector('.meteor__img');
        const cap = gallery.querySelector('.meteor__caption');
        const details = gallery.querySelector('.meteor__details');
        const summary = gallery.querySelector('.meteor__summary');
        const meta = gallery.querySelector('.meteor__meta');
        const cmd = gallery.querySelector('.meteor__command');
        if (details) details.open = true;
        if (img) {
          if (!img.complete) {
            await new Promise(resolve => { img.onload = resolve; img.onerror = resolve; });
          }
          imageLoaded = img.naturalWidth > 0 && img.naturalHeight > 0;
          if (expectedTheme === 'red-night') {
            redNightFiltered = getComputedStyle(img).filter.includes('gallery-ink-red');
          }
        }
        for (const el of [cap, summary, meta, cmd]) {
          if (!el) continue;
          minFontPx = Math.min(minFontPx, parseFloat(getComputedStyle(el).fontSize));
          if (el.scrollWidth > el.clientWidth + 1) noHorizOverflow = false;
        }
        gallery.scrollTop = gallery.scrollHeight;
        if (cmd) {
          const gR = gallery.getBoundingClientRect();
          const cR = cmd.getBoundingClientRect();
          detailsVisible = cR.height > 0 && cR.bottom <= gR.bottom + 2 &&
                           cmd.scrollWidth <= cmd.clientWidth + 1;
        }
      }
    }
  } finally {
    window.fetch = origFetch;
  }

  const docSize = [document.documentElement.scrollWidth, document.documentElement.scrollHeight];
  return {
    theme: app.dataset.theme,
    doc: allDocFit ? [800, 480] : docSize,
    back: allBackFit,
    minBackH: Math.round(minBackH),
    listen: allListenFit,
    minListenH: Math.round(minListenH),
    listenOutsideGallery,
    noHorizOverflow,
    unclippedText,
    maxPhaseLen,
    maxNoteLen,
    minFontPx,
    imageLoaded,
    detailsVisible,
    redNightFiltered,
    statesChecked: cases.length,
  };
}
"""


def load_page(browser, url):
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
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        meteor_url = f"{console}/operate/meteor"
        load_page(browser, meteor_url)
        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            load_page(browser, meteor_url)
            expr = (
                f"{METEOR_CHECK}\n"
                f"measureMeteor({json.dumps(theme)}, {json.dumps(SAMPLE_PNG_URI)}, "
                f"{json.dumps(LONG_CMD)})"
            )
            reply = browser.call("Runtime.evaluate",
                                 {"expression": expr, "returnByValue": True, "awaitPromise": True})
            measures = reply.get("result", {}).get("value") or {}
            ok = (
                measures.get("theme") == theme
                and measures.get("doc") == [800, 480]
                and measures.get("back") is True
                and measures.get("listen") is True
                and measures.get("listenOutsideGallery") is True
                and measures.get("noHorizOverflow") is True
                and measures.get("unclippedText") is True
                and measures.get("maxPhaseLen", 999) < 65
                and measures.get("maxNoteLen", 999) < 120
                and measures.get("minFontPx", 0) >= 18
                and measures.get("imageLoaded") is True
                and measures.get("detailsVisible") is True
                and measures.get("redNightFiltered") is True
                and measures.get("statesChecked", 0) == 8
            )
            failed |= verdict(ok, f"meteor {theme}", measures)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
