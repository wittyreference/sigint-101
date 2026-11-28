# ABOUTME: Check M5's 12-card legal sort screen and /law reading page at 800x480 over DevTools.
# ABOUTME: Verifies card-sort fit (every card, each with its miss, 80 px buttons) and /law scroll.

import json
import sys
import time

from render_scroll import connect, fence, leave_page

THEMES = ("dark", "sunlight", "red-night")

CARD_SORT = """
function measureCardSort(expectedTheme) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back');
  const panels = [...document.querySelectorAll('.stage-panel')];
  const panel = document.querySelector('.stage-panel[data-stage="try"][data-sort-panel]');
  if (!app || !back || !panel) return null;

  panels.forEach(p => { p.hidden = (p !== panel); });

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

  const choices = [...panel.querySelectorAll('[data-sort-choice]')];
  const footBtns = [...panel.querySelectorAll('.stage-foot button, .stage-foot a')];
  const intro = panel.querySelector('[data-sort-intro]');
  const card = panel.querySelector('.sort-card');
  const text = panel.querySelector('[data-sort-text]');
  const fb = panel.querySelector('[data-sort-feedback]');
  const tag = panel.querySelector('[data-sort-tag]');
  const deckEl = panel.querySelector('[data-sort-deck]');
  const prevBtn = panel.querySelector('[data-sort-prev]');
  const nextBtn = panel.querySelector('[data-sort-next]');
  if (choices.length !== 3 || !card || !text || !fb || !deckEl || !prevBtn || !nextBtn) {
    return null;
  }
  const introFontPx = intro ? Math.round(parseFloat(getComputedStyle(intro).fontSize) || 0) : 0;
  // The deck the page was served, which card-sort.js steps through: the walk must reach all of it.
  let deck = [];
  try { deck = JSON.parse(deckEl.textContent || '[]'); } catch (_) { deck = []; }
  if (!Array.isArray(deck)) deck = [];
  const shown = () => (text.textContent || '').trim();
  const missShown = () => !fb.hidden && !!fb.classList && fb.classList.contains('verdict--miss') &&
                          !!tag && (tag.textContent || '').trim() === 'Missed';

  // Stub fetch before clicking so the render check never POSTs /api/sort to a live console.
  const origFetch = window.fetch;
  window.fetch = () => new Promise(() => {});

  let choicesFit = true;
  let footerFit = true;
  let feedbackFits = true;
  let cardScrolls = false;
  let cardsChecked = 0;
  let missesChecked = 0;
  let worstDelta = 0;
  let maxDocW = document.documentElement.scrollWidth;
  let maxDocH = document.documentElement.scrollHeight;
  let choiceHeights = choices.map(b => Math.round(b.getBoundingClientRect().height));

  try {
    // Walk back to card 1, then visit every card of the deck and tap all 3 choices to measure
    // every state. Two of them are wrong, so each card is measured with its "Missed" feedback,
    // the longest text it shows. A card counts only once the page shows its text, so a step
    // that does nothing or a Next card that stays disabled ends the walk short, and fails it.
    for (let k = 0; k < deck.length && !prevBtn.disabled; k += 1) prevBtn.click();

    for (let i = 0; i < deck.length; i += 1) {
      const entry = deck[i] || {};
      if (shown() !== String(entry.text || '').trim()) break;
      let missed = false;
      for (const btn of choices) {
        btn.click();
        if (missShown() && btn.getAttribute('data-sort-choice') !== entry.answer) missed = true;
        maxDocW = Math.max(maxDocW, document.documentElement.scrollWidth);
        maxDocH = Math.max(maxDocH, document.documentElement.scrollHeight);
        const heights = choices.map(b => Math.round(b.getBoundingClientRect().height));
        choiceHeights = choiceHeights.map((h, idx) => Math.min(h, heights[idx]));
        if (!choices.every(unclipped)) choicesFit = false;
        if (!footBtns.every(b => unclipped(b) && b.getBoundingClientRect().height >= 72)) {
          footerFit = false;
        }
        const fbRect = fb.getBoundingClientRect();
        if (fb.hidden || !inViewport(fbRect)) feedbackFits = false;
        const delta = card.scrollHeight - card.clientHeight;
        worstDelta = Math.max(worstDelta, delta);
        if (delta > 0) cardScrolls = true;
      }
      cardsChecked += 1;
      if (missed) missesChecked += 1;
      if (i === deck.length - 1 || nextBtn.disabled) break;
      nextBtn.click();
    }
  } finally {
    window.fetch = origFetch;
  }

  const backRect = back.getBoundingClientRect();
  return {
    theme: app.dataset.theme,
    doc: [maxDocW, maxDocH],
    back: inViewport(backRect) && backRect.height >= 72,
    introFontPx,
    choicesFit,
    choiceHeights,
    footerFit,
    feedbackFits,
    cardScrolls,
    deckSize: deck.length,
    cardsChecked,
    missesChecked,
    worstDelta,
  };
}
"""

LAW = """
function measureLaw(expectedTheme) {
  const app = document.querySelector('.app');
  const back = document.querySelector('.btn-back[href="/lesson/5"]');
  const list = document.querySelector('.law__entries');
  const sections = [...document.querySelectorAll('.law__section')];
  const last = sections[sections.length - 1];
  if (!app || !back || !list || !last) return null;

  function inViewport(element) {
    const r = element.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
           r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 0.5;
  }

  const cs = getComputedStyle(list);
  const scrolls = cs.overflowY === 'auto' &&
                  list.scrollHeight > list.clientHeight &&
                  list.scrollWidth <= list.clientWidth;
  const backBefore = inViewport(back);
  list.scrollTop = list.scrollHeight;
  const end = last.getBoundingClientRect().bottom;
  return {
    theme: app.dataset.theme,
    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    back: backBefore && inViewport(back) && back.getBoundingClientRect().height >= 72,
    scrolls,
    lastVisible: list.scrollTop > 0 && end <= list.getBoundingClientRect().bottom + 1,
    sections: sections.length,
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
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride",
                     {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False})
        lesson_url = f"{console}/lesson/5"
        law_url = f"{console}/law?back=/lesson/5"
        load_page(browser, lesson_url)
        failed = False
        for theme in THEMES:
            browser.ev(f"localStorage.setItem('sigint101.theme', {json.dumps(theme)})")
            load_page(browser, lesson_url)
            sort = browser.ev(f"{CARD_SORT}\nmeasureCardSort({json.dumps(theme)})") or {}
            sort_ok = (
                sort.get("theme") == theme
                and sort.get("doc") == [800, 480]
                and sort.get("back") is True
                and sort.get("introFontPx", 0) >= 18
                and sort.get("choicesFit") is True
                and len(sort.get("choiceHeights", [])) == 3
                and all(72 <= h <= 80 for h in sort["choiceHeights"])
                and sort.get("footerFit") is True
                and sort.get("feedbackFits") is True
                and sort.get("cardScrolls") is False
                and sort.get("worstDelta") == 0
                # It walked the whole deck the page was served, and saw each card's miss.
                and type(sort.get("deckSize")) is int and sort["deckSize"] > 0
                and sort.get("cardsChecked") == sort["deckSize"]
                and sort.get("missesChecked") == sort["deckSize"]
            )
            failed |= verdict(sort_ok, f"card-sort {theme}", sort)

            load_page(browser, law_url)
            law = browser.ev(f"{LAW}\nmeasureLaw({json.dumps(theme)})") or {}
            law_ok = (
                law.get("theme") == theme
                and law.get("doc") == [800, 480]
                and law.get("back") is True
                and law.get("scrolls") is True
                and law.get("lastVisible") is True
                and law.get("sections", 0) > 0
            )
            failed |= verdict(law_ok, f"law {theme}", law)
        return int(failed)
    finally:
        leave_page(browser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
