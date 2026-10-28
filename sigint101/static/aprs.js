// ABOUTME: Lists live APRS stations on the console by polling /api/aprs (M14's packet lesson).
// ABOUTME: On 503 it shows the reason and never invents a station — absence is an honest answer.

(function () {
  "use strict";

  // The short tag for a packet's kind, shown beside the callsign.
  function kindLabel(kind) {
    if (kind === "position") return "POS";
    if (kind === "message") return "MSG";
    if (kind === "status") return "STATUS";
    return "·";
  }

  // The one line a learner reads for a station's latest packet: a decoded position, a message to
  // someone, a status, or whatever text came through. Pure, so it is unit-tested without a DOM.
  function summary(s) {
    s = s || {};
    if (s.kind === "position" && s.lat != null && s.lon != null) {
      var pos = s.lat.toFixed(3) + ", " + s.lon.toFixed(3);
      return s.text ? pos + " · " + s.text : pos;
    }
    if (s.kind === "message") {
      var to = s.addressee ? "→" + s.addressee + ": " : "";
      return to + (s.text || "");
    }
    return s.text || s.dest || "";
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { kindLabel: kindLabel, summary: summary };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-aprs-panel]"));
  if (!panels.length) return;

  var POLL_MS = 2000;      // APRS beacons are seconds-to-minutes apart; a steady poll is plenty
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-aprs-list]");
    var empty = panel.querySelector("[data-aprs-empty]");
    var badge = panel.querySelector("[data-aprs-badge]");
    var stations = body.stations || [];
    list.innerHTML = "";
    stations.forEach(function (s) {
      var li = document.createElement("li");
      li.className = "decoder__item decoder__item--" + (s.kind || "other");
      li.innerHTML =
        '<span class="decoder__model">' + esc(s.source) + "</span>" +
        '<span class="decoder__mod">' + esc(kindLabel(s.kind)) + "</span>" +
        '<span class="decoder__read">' + esc(summary(s)) + "</span>" +
        '<span class="decoder__tone">×' + esc(s.packet_count) + "</span>";
      list.appendChild(li);
    });
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (empty) {
      empty.hidden = stations.length > 0;
      if (!stations.length) empty.textContent = "listening… no stations heard yet";
    }
  }

  function showEmpty(panel, reason) {
    var list = panel.querySelector("[data-aprs-list]");
    var empty = panel.querySelector("[data-aprs-empty]");
    var badge = panel.querySelector("[data-aprs-badge]");
    if (list) list.innerHTML = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode APRS"; }
  }

  function tick() {
    fetch("/api/aprs", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode APRS";
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
