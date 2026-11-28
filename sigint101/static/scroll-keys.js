// ABOUTME: Keyboard scrolling for the Nomad's keys on every screen — the active lesson stage body,
// ABOUTME: exam card, tile grid or operator picker. Updates the scroll cues and keeps the bottom line whole.

(function () {
  "use strict";

  // Pure: the scroll a key means, given shift state and the band's visible height. Returns
  // {dy:px} for a relative move, {to:"top"|"bottom"} for a jump, or null for a key we ignore.
  // Extracted so it is testable without a DOM.
  function scrollAction(key, shiftKey, clientHeight) {
    var step = 48, page = Math.max(80, (clientHeight || 0) - 48);
    switch (key) {
      case "ArrowDown": return { dy: step };
      case "ArrowUp": return { dy: -step };
      case "PageDown": return { dy: page };
      case "PageUp": return { dy: -page };
      case "Home": return { to: "top" };
      case "End": return { to: "bottom" };
      case " ": return { dy: shiftKey ? -page : page };
      default: return null;
    }
  }

  // Under node's test runner the file is loaded for its pure helper and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { scrollAction: scrollAction };
    return;
  }

  // Clip at the start of a line that crosses the bottom edge. The inner copy keeps its layout
  // and scroll range, so the whole line can come into view on the next scroll.
  function trimPartialLine(band, copy) {
    var clip = "";
    if (band.clientHeight > 0) {
      var edge = band.getBoundingClientRect().top + band.clientTop + band.clientHeight;
      var cutoff = edge;
      var walker = document.createTreeWalker(copy, NodeFilter.SHOW_TEXT);
      var range = document.createRange(), node;
      while ((node = walker.nextNode())) {
        range.selectNodeContents(node);
        for (var line of range.getClientRects()) {
          if (line.top < edge && line.bottom > edge) cutoff = Math.min(cutoff, line.top);
        }
      }
      if (cutoff < edge) {
        clip = "inset(0 0 " + (copy.getBoundingClientRect().bottom - cutoff) + "px 0)";
      }
    }
    copy.style.clipPath = clip;
  }

  // A tile grid scrolls on its own; bring the current unit or lesson into view when the page opens.
  var tileGrid = document.querySelector && document.querySelector(".tiles");
  var activeTile = tileGrid && tileGrid.querySelector(".tile--active");
  if (activeTile) activeTile.scrollIntoView({ block: "center", inline: "nearest" });

  document.querySelectorAll(".scroll-frame").forEach(function (frame) {
    var band = frame.firstElementChild;
    var copy = band.querySelector(".scroll-copy");
    function refresh() {
      var room = band.scrollHeight - band.clientHeight;
      var tolerance = copy ? 0.5 : 2;
      var scrollable = band.clientHeight > 0 && room > tolerance;
      var more = scrollable && room - band.scrollTop > tolerance;
      frame.toggleAttribute("data-scrollable", scrollable);
      frame.toggleAttribute("data-more-below", more);
      if (copy) trimPartialLine(band, copy);
    }
    band.addEventListener("scroll", refresh, { passive: true });
    if (typeof ResizeObserver !== "undefined") {
      var observer = new ResizeObserver(refresh);
      observer.observe(band);
      if (copy) observer.observe(copy);
    }
    if (document.fonts) document.fonts.ready.then(refresh);
    refresh();
  });

  // The one scroll region in play on this screen: the visible, actually-overflowing stage body on
  // a lesson, the exam card's body, the tile grid or the operator picker.
  function region() {
    var panel = document.querySelector(".stage-panel:not([hidden])");
    if (panel) {
      var bands = panel.querySelectorAll(".stage-panel__body");
      for (var i = 0; i < bands.length; i++) {
        var el = bands[i], cs = getComputedStyle(el);
        if ((cs.overflowY === "auto" || cs.overflowY === "scroll") &&
            el.scrollHeight > el.clientHeight) return el;
      }
    }
    var exam = document.querySelector(".exam__body");
    if (exam && exam.scrollHeight - exam.clientHeight > 2) return exam;
    var tiles = document.querySelector(".tiles");
    if (tiles && tiles.scrollHeight - tiles.clientHeight > 2) return tiles;
    var picker = document.querySelector(".rack__picker");
    if (picker && picker.scrollHeight - picker.clientHeight > 2) return picker;
    var law = document.querySelector(".law__entries");
    if (law && law.scrollHeight - law.clientHeight > 2) return law;
    var bandmap = document.querySelector(".bandmap__entries");
    if (bandmap && bandmap.scrollHeight - bandmap.clientHeight > 2) return bandmap;
    var field = document.querySelector(".field__entries");
    if (field && field.scrollHeight - field.clientHeight > 2) return field;
    return null;
  }

  document.addEventListener("keydown", function (ev) {
    var ae = document.activeElement;
    // Let the tap chips (a lesson's prediction, an exam's choice) keep arrow selection, and a
    // focused button keep Space, when focused.
    var chip = ae && (ae.name === "prediction" || ae.name === "choice");
    if (chip && (ev.key === "ArrowUp" || ev.key === "ArrowDown")) return;
    if (ev.key === " " && ae && /^(BUTTON|A|INPUT)$/.test(ae.tagName)) return;
    if (!scrollAction(ev.key, ev.shiftKey, 0)) return;   // not one of our keys
    var band = region();
    if (!band) return;
    var act = scrollAction(ev.key, ev.shiftKey, band.clientHeight);
    if (act.to === "top") band.scrollTo(0, 0);
    else if (act.to === "bottom") band.scrollTo(0, band.scrollHeight);
    else band.scrollBy(0, act.dy);
    ev.preventDefault();
  });
})();
