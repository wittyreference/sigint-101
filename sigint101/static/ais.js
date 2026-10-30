// ABOUTME: Lists live AIS vessels on the console by polling /api/ais (M26's ship-tracking lesson).
// ABOUTME: On 503 it shows the reason and never invents a vessel — an empty sea is an honest answer.

(function () {
  "use strict";

  // How a vessel reads as a line: its name once the static message arrives, else its MMSI.
  function ident(vessel) {
    vessel = vessel || {};
    return vessel.name ? vessel.name : "MMSI " + vessel.mmsi;
  }

  // The position the vessel last reported, to four places, or a note while only static data is in.
  function position(vessel) {
    vessel = vessel || {};
    if (vessel.lat == null || vessel.lon == null) return "no position yet";
    return Number(vessel.lat).toFixed(4) + ", " + Number(vessel.lon).toFixed(4);
  }

  // The way it is making: speed over ground and course, each shown only when the ship reported it.
  function motion(vessel) {
    vessel = vessel || {};
    var parts = [];
    if (vessel.sog_kt != null) parts.push(Number(vessel.sog_kt).toFixed(1) + " kt");
    if (vessel.cog_deg != null) parts.push(Number(vessel.cog_deg).toFixed(0) + "°");
    return parts.join(" · ");
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { ident: ident, position: position, motion: motion };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-ais-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;      // vessels beacon on a schedule; a steady poll keeps the roster current
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-ais-list]");
    var empty = panel.querySelector("[data-ais-empty]");
    var badge = panel.querySelector("[data-ais-badge]");
    var vessels = body.vessels || [];
    list.innerHTML = "";
    vessels.forEach(function (v) {
      var li = document.createElement("li");
      li.className = "decoder__item";
      li.innerHTML =
        '<span class="decoder__model">' + esc(ident(v)) + "</span>" +
        '<span class="decoder__mod">' + esc(v.nav_status || "") + "</span>" +
        '<span class="decoder__read">' + esc(position(v)) + "</span>" +
        '<span class="decoder__tone">' + esc(motion(v)) + "</span>";
      list.appendChild(li);
    });
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (empty) {
      empty.hidden = vessels.length > 0;
      if (!vessels.length) empty.textContent = "listening… no vessels heard yet";
    }
  }

  function showEmpty(panel, reason) {
    var list = panel.querySelector("[data-ais-list]");
    var empty = panel.querySelector("[data-ais-empty]");
    var badge = panel.querySelector("[data-ais-badge]");
    if (list) list.innerHTML = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode AIS"; }
  }

  function tick() {
    fetch("/api/ais?limit=12", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode AIS";
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
