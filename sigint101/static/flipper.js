// ABOUTME: Shows the attached Flipper's identity on the console by polling /api/flipper (M29).
// ABOUTME: On 503 it shows the reason, on any other failure its status; it never invents a device.

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

  // The device's account of itself, as the route sends it: an object, never a bare value.
  function isDevice(device) {
    return !!device && typeof device === "object" && !Array.isArray(device);
  }

  // The answer a poll is shown from. The route's own answers are a reading (available, with the
  // device's account) or a refusal with its reason; anything else is no reading, said with its
  // HTTP status: a server fault's page (not JSON), a JSON error with no reason, a page from
  // something in between, a 200 with no device in it. Left alone, such an answer kept the panel
  // on "reading the Flipper over USB…" for as long as it was open, read as "no Flipper on USB",
  // or drew a card for a device nobody reported. status null: no answer came at all.
  function pollAnswer(status, body) {
    var ok = status >= 200 && status < 300;
    if (body && typeof body === "object") {
      if (body.available === false && typeof body.reason === "string" && body.reason) return body;
      if (ok && body.available === true && isDevice(body.device)) return body;
    }
    return { available: false, command: null, device: null,
             reason: status ? "the console could not read the Flipper (HTTP " + status + ")"
                            : "lost contact with the console" };
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { deviceLine: deviceLine, radioLine: radioLine, pollAnswer: pollAnswer };
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
  // bus, an app open on it, a port that failed, an answer that was no reading). The badge says only
  // that nothing was read, which is true of all of them.
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
      .then(function (r) {
        return r.json().then(function (b) { return pollAnswer(r.status, b); },
                             function () { return pollAnswer(r.status, null); });
      }, function () { return pollAnswer(null, null); })
      .then(function (body) {
        var waiting = body.available === false;
        panels.forEach(function (p) { if (waiting) showEmpty(p, body); else render(p, body); });
        if (!stopped) setTimeout(tick, waiting ? RETRY_MS : POLL_MS);
      })
      .catch(function () { if (!stopped) setTimeout(tick, RETRY_MS); });
  }

  tick();
})();
