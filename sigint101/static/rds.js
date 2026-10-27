// ABOUTME: Fills the RDS station card on the console by polling /api/rds (M12's lesson).
// ABOUTME: On 503 it shows the reason and never invents a station — absence is an honest answer.

(function () {
  "use strict";

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  // Export them and stop before any DOM access; in the browser there is no `module`, so it runs.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { hasData: hasData, psOrDash: psOrDash };
    return;
  }

  // The station has shown something worth revealing once any group carried identity or text.
  function hasData(station) {
    if (!station) return false;
    return !!(station.ps || station.pi || station.radiotext || station.prog_type);
  }

  function psOrDash(station) {
    return (station && station.ps) ? station.ps : "—";
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-rds-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;      // RDS trickles in a few bits per group; a steady poll is plenty
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var stopped = false;

  function set(panel, sel, value) {
    var el = panel.querySelector(sel);
    if (el) el.textContent = (value == null || value === "") ? "—" : String(value);
  }

  function render(panel, body) {
    var station = body.station || {};
    var card = panel.querySelector("[data-rds-card]");
    var empty = panel.querySelector("[data-rds-empty]");
    var badge = panel.querySelector("[data-rds-badge]");
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (hasData(station)) {
      set(panel, "[data-rds-ps]", psOrDash(station));
      set(panel, "[data-rds-pi]", station.pi);
      set(panel, "[data-rds-pty]", station.prog_type);
      set(panel, "[data-rds-count]", station.group_count || 0);
      var rt = panel.querySelector("[data-rds-rt]");
      if (rt) rt.textContent = station.radiotext || "";
      if (card) card.hidden = false;
      if (empty) empty.hidden = true;
    } else {
      if (card) card.hidden = true;
      if (empty) {
        empty.hidden = false;
        empty.textContent = "listening… no RDS decoded yet";
      }
    }
  }

  function showEmpty(panel, reason) {
    var card = panel.querySelector("[data-rds-card]");
    var empty = panel.querySelector("[data-rds-empty]");
    var badge = panel.querySelector("[data-rds-badge]");
    if (card) card.hidden = true;
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode RDS"; }
  }

  function tick() {
    fetch("/api/rds", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode RDS";
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
