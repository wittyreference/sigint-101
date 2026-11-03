// ABOUTME: Shows the attached Flipper's identity on the console by polling /api/flipper (M29).
// ABOUTME: On 503 it shows the reason (no Flipper on USB) and never invents a device — absence is honest.

(function () {
  "use strict";

  // The one-line identity: model, firmware, and region when the device reported one.
  function deviceLine(info) {
    info = info || {};
    var parts = [];
    if (info.model) parts.push(info.model);
    if (info.firmware) parts.push("fw " + info.firmware);
    if (info.region) parts.push(info.region);
    return parts.join(" · ");
  }

  // The radio state as words (state by words, never colour alone), or "" when unknown.
  function radioLine(info) {
    info = info || {};
    if (info.radio_alive === true) return "radio: alive";
    if (info.radio_alive === false) return "radio: down";
    return "";
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { deviceLine: deviceLine, radioLine: radioLine };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-flipper-panel]"));
  if (!panels.length) return;

  var POLL_MS = 2000;      // device identity is static; a gentle poll keeps "plugged in?" current
  var RETRY_MS = 4000;     // while no Flipper is attached, poll gently until one appears
  var stopped = false;

  // The device card's text, all of it from the device; cleared when the device goes, so a Flipper
  // that was unplugged never lingers on the panel.
  function fillCard(panel, info) {
    var name = panel.querySelector("[data-flipper-name]");
    var line = panel.querySelector("[data-flipper-line]");
    var radio = panel.querySelector("[data-flipper-radio]");
    if (name) name.textContent = info ? (info.name || "Flipper") : "";
    if (line) line.textContent = info ? deviceLine(info) : "";
    if (radio) radio.textContent = info ? radioLine(info) : "";
  }

  // No read: the card hides and the empty line carries the reason (no Flipper on USB, two on the
  // bus, an app open on it). The badge says only that nothing was read, which is true of all three.
  function showEmpty(panel, body) {
    var card = panel.querySelector("[data-flipper-card]");
    var empty = panel.querySelector("[data-flipper-empty]");
    var badge = panel.querySelector("[data-flipper-badge]");
    fillCard(panel, null);
    if (card) card.hidden = true;
    DecoderPanel.showDown(panel, badge, empty, body, "NO READ", "no Flipper on USB");
  }

  function render(panel, body) {
    var card = panel.querySelector("[data-flipper-card]");
    var empty = panel.querySelector("[data-flipper-empty]");
    var badge = panel.querySelector("[data-flipper-badge]");
    fillCard(panel, body.device || {});
    if (card) card.hidden = false;
    if (empty) empty.hidden = true;
    DecoderPanel.showLive(badge, body);
    DecoderPanel.showCommand(panel, body.command);
  }

  function tick() {
    fetch("/api/flipper", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          panels.forEach(function (p) { showEmpty(p, res.body); });
          waiting = true;
        } else {
          panels.forEach(function (p) { render(p, res.body); });
        }
        if (!stopped) setTimeout(tick, waiting ? RETRY_MS : POLL_MS);
      })
      .catch(function () { if (!stopped) setTimeout(tick, RETRY_MS); });
  }

  tick();
})();
