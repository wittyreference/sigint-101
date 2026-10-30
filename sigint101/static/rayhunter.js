// ABOUTME: Shows the Orbic Rayhunter recording line + heuristic findings by polling /api/rayhunter
// ABOUTME: (M19). On 503 it shows the reason; a quiet line never proves no catcher — red = look closer.

(function () {
  "use strict";

  // The one-glance recording line the device shows: green clean, red warning, grey not-recording.
  function lineText(status) {
    if (status === "clean") return "recording · clean";
    if (status === "warning") return "warning · look closer";
    if (status === "off") return "not recording";
    return "—";
  }

  function lineClass(status) {
    var base = "rayhunter__line";
    if (status === "clean") return base + " rayhunter__line--clean";
    if (status === "warning") return base + " rayhunter__line--warning";
    return base;
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { lineText: lineText, lineClass: lineClass };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-rayhunter-panel]"));
  if (!panels.length) return;

  var POLL_MS = 3000;      // Rayhunter analyses continuously; a steady poll keeps the line current
  var RETRY_MS = 6000;     // when the Orbic is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var line = panel.querySelector("[data-rayhunter-line]");
    var list = panel.querySelector("[data-rayhunter-list]");
    var empty = panel.querySelector("[data-rayhunter-empty]");
    var badge = panel.querySelector("[data-rayhunter-badge]");
    var status = body.status || "off";
    var warnings = body.warnings || [];
    if (line) { line.textContent = lineText(status); line.className = lineClass(status); }
    if (list) {
      list.innerHTML = "";
      warnings.forEach(function (w) {
        var li = document.createElement("li");
        li.className = "decoder__item";
        li.innerHTML =
          '<span class="decoder__model">' + esc(w.name) + "</span>" +
          '<span class="decoder__mod">' + esc(w.severity) + "</span>" +
          '<span class="decoder__read">' + esc(w.description) + "</span>" +
          '<span class="decoder__tone">' + esc(w.packet == null ? "" : "pkt " + w.packet) + "</span>";
        list.appendChild(li);
      });
    }
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (empty) {
      empty.hidden = warnings.length > 0;
      if (!warnings.length) empty.textContent = "no findings — a quiet line never proves no catcher";
    }
  }

  function showEmpty(panel, reason) {
    var line = panel.querySelector("[data-rayhunter-line]");
    var list = panel.querySelector("[data-rayhunter-list]");
    var empty = panel.querySelector("[data-rayhunter-empty]");
    var badge = panel.querySelector("[data-rayhunter-badge]");
    if (line) { line.textContent = "orbic unreachable"; line.className = "rayhunter__line"; }
    if (list) list.innerHTML = "";
    if (badge) { badge.textContent = "NO ORBIC"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no Orbic to read Rayhunter"; }
  }

  function tick() {
    fetch("/api/rayhunter?limit=20", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no Orbic to read Rayhunter";
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
