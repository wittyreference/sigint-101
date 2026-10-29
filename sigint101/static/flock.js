// ABOUTME: Lists live Flock-camera detections on the console by polling /api/flock (M18's lesson).
// ABOUTME: On 503 it shows the reason and never invents a detection — no alert never means no camera.

(function () {
  "use strict";

  // The evidence tier as a short confidence word. Higher is stronger: a bare OUI is a hint, the
  // OUI plus the exact IE fingerprint is a hit. The label is the lesson's whole point.
  function tierLabel(tier) {
    switch (tier) {
      case 4: return "STRONG HIT";
      case 3: return "wildcard probe";
      case 2: return "OUI match";
      case 1: return "weak (addr)";
      case 0: return "SSID keyword";
      default: return "—";
    }
  }

  // Which device the evidence named: its MAC, falling back to the OUI alone.
  function ident(d) {
    d = d || {};
    return d.mac ? d.mac : (d.oui || "—");
  }

  // The one line a learner reads: why it fired, and any network name it was hunting for.
  function summary(d) {
    d = d || {};
    var line = d.rule || "";
    if (d.ssid) line += (line ? " · " : "") + '"' + d.ssid + '"';
    return line || "(no detail)";
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { tierLabel: tierLabel, ident: ident, summary: summary };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-flock-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;      // management frames arrive in bursts; a steady poll keeps the list current
  var RETRY_MS = 5000;     // when the monitor radio is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-flock-list]");
    var empty = panel.querySelector("[data-flock-empty]");
    var badge = panel.querySelector("[data-flock-badge]");
    var detections = body.detections || [];
    list.innerHTML = "";
    detections.forEach(function (d) {
      var li = document.createElement("li");
      li.className = "decoder__item";
      li.innerHTML =
        '<span class="decoder__model">' + esc(ident(d)) + "</span>" +
        '<span class="decoder__mod">' + esc(tierLabel(d.tier)) + "</span>" +
        '<span class="decoder__read">' + esc(summary(d)) + "</span>" +
        '<span class="decoder__tone">' + esc("×" + (d.count || 0)) + "</span>";
      list.appendChild(li);
    });
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (empty) {
      empty.hidden = detections.length > 0;
      if (!detections.length) empty.textContent = "listening… no Flock signatures heard yet";
    }
  }

  function showEmpty(panel, reason) {
    var list = panel.querySelector("[data-flock-list]");
    var empty = panel.querySelector("[data-flock-empty]");
    var badge = panel.querySelector("[data-flock-badge]");
    if (list) list.innerHTML = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no monitor radio to watch for Flock"; }
  }

  function tick() {
    fetch("/api/flock?limit=20", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no monitor radio to watch for Flock";
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
