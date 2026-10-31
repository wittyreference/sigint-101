// ABOUTME: Shows the attached Flipper's identity on the console by polling /api/flipper (M29).
// ABOUTME: On 503 it shows "plug the Flipper in" and never invents a device — absence is honest.

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

  function showEmpty(panel, reason) {
    var name = panel.querySelector("[data-flipper-name]");
    var line = panel.querySelector("[data-flipper-line]");
    var radio = panel.querySelector("[data-flipper-radio]");
    if (name) name.textContent = "No Flipper";
    if (line) line.textContent = reason || "plug the Flipper into the nomad";
    if (radio) radio.textContent = "";
    panel.classList.add("flipper--empty");
  }

  function render(panel, body) {
    var info = body.device || {};
    var name = panel.querySelector("[data-flipper-name]");
    var line = panel.querySelector("[data-flipper-line]");
    var radio = panel.querySelector("[data-flipper-radio]");
    if (name) name.textContent = info.name || "Flipper";
    if (line) line.textContent = deviceLine(info);
    if (radio) radio.textContent = radioLine(info);
    panel.classList.remove("flipper--empty");
  }

  function tick() {
    fetch("/api/flipper", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "plug the Flipper into the nomad";
          panels.forEach(function (p) { showEmpty(p, reason); });
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
