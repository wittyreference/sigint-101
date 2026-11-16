// ABOUTME: Keyboard scrolling for the Nomad's keys on every screen — the active lesson stage body,
// ABOUTME: the exam card or the home grid. Arrows/PageUp-Down/Space/Home/End; arrows yield to chips.

(function () {
  "use strict";

  // Pure: the scroll a key means, given shift state and the band's visible height. Returns
  // {dy:px} for a relative move, {to:"top"|"bottom"} for a jump, or null for a key we ignore.
  // Extracted so it is testable without a DOM (the keydown wiring below is the only impure part).
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

  // The one scroll region in play on this screen: the visible, actually-overflowing stage body on
  // a lesson; the exam card's body; the tile grid on home. Null when nothing overflows (short
  // page — keys do nothing).
  function region() {
    var panel = document.querySelector(".stage-panel:not([hidden])");
    if (panel) {
      var bands = panel.querySelectorAll(".stage-panel__body");
      for (var i = 0; i < bands.length; i++) {
        var el = bands[i], cs = getComputedStyle(el);
        if ((cs.overflowY === "auto" || cs.overflowY === "scroll") &&
            el.scrollHeight - el.clientHeight > 2) return el;
      }
    }
    var exam = document.querySelector(".exam__body");
    if (exam && exam.scrollHeight - exam.clientHeight > 2) return exam;
    var tiles = document.querySelector(".tiles");
    if (tiles && tiles.scrollHeight - tiles.clientHeight > 2) return tiles;
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
