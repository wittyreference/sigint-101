// ABOUTME: Draws the ADS-B radar scope on the console by polling /api/adsb (M13's lesson).
// ABOUTME: On 503 or an empty sky it says so and never invents a contact — absence is honest.

(function () {
  "use strict";

  var VIEW = 200;          // SVG viewBox is 0..200 in both axes
  var CENTER = 100;        // the receiver sits at the middle
  var MAX_R = 90;          // the outer ring's radius, matching the scope skeleton
  // The ring distances the scope will round its scale up to, so the rings read in round numbers.
  var NICE_RANGES = [10, 25, 50, 100, 150, 200, 300];

  // Pick the outer-ring distance (nm): the smallest nice range that holds the furthest located
  // aircraft, defaulting to 50 nm when nothing is placed yet so the empty scope still reads.
  function scopeRange(aircraft) {
    var furthest = 0;
    (aircraft || []).forEach(function (a) {
      if (a.range_nm != null && a.range_nm > furthest) furthest = a.range_nm;
    });
    if (furthest <= 0) return 50;
    for (var i = 0; i < NICE_RANGES.length; i++) {
      if (NICE_RANGES[i] >= furthest) return NICE_RANGES[i];
    }
    return NICE_RANGES[NICE_RANGES.length - 1];
  }

  // Place one contact on the scope: range sets the radius, bearing the angle (0 deg = north = up,
  // 90 deg = east = right). Range beyond the outer ring is pinned to the edge, never dropped.
  function plot(rangeNm, bearingDeg, maxRangeNm) {
    var frac = maxRangeNm > 0 ? Math.min(rangeNm / maxRangeNm, 1) : 0;
    var radius = frac * MAX_R;
    var rad = (bearingDeg || 0) * Math.PI / 180;
    return {
      x: CENTER + radius * Math.sin(rad),
      y: CENTER - radius * Math.cos(rad)
    };
  }

  function planeLabel(a) {
    return (a && a.callsign) ? a.callsign : (a && a.hex_ident) || "";
  }

  function hasAircraft(body) {
    return !!(body && body.aircraft && body.aircraft.length);
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { scopeRange: scopeRange, plot: plot,
                       planeLabel: planeLabel, hasAircraft: hasAircraft };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-adsb-panel]"));
  if (!panels.length) return;

  var SVGNS = "http://www.w3.org/2000/svg";
  var POLL_MS = 1500;      // squitters arrive every second or two; a steady poll keeps up
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var stopped = false;

  function clear(group) {
    while (group && group.firstChild) group.removeChild(group.firstChild);
  }

  function drawDot(group, pos, label) {
    var dot = document.createElementNS(SVGNS, "circle");
    dot.setAttribute("class", "radar__plane");
    dot.setAttribute("cx", pos.x.toFixed(1));
    dot.setAttribute("cy", pos.y.toFixed(1));
    dot.setAttribute("r", "2.6");
    group.appendChild(dot);
    if (label) {
      var text = document.createElementNS(SVGNS, "text");
      text.setAttribute("class", "radar__plane-label");
      text.setAttribute("x", (pos.x + 4).toFixed(1));
      text.setAttribute("y", (pos.y - 3).toFixed(1));
      text.textContent = label;
      group.appendChild(text);
    }
  }

  function render(panel, body) {
    var aircraft = body.aircraft || [];
    var group = panel.querySelector("[data-adsb-dots]");
    var empty = panel.querySelector("[data-adsb-empty]");
    var badge = panel.querySelector("[data-adsb-badge]");
    var count = panel.querySelector("[data-adsb-count]");
    var ring = panel.querySelector("[data-adsb-ring]");
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (count) count.textContent = String(aircraft.length);
    clear(group);

    var located = aircraft.filter(function (a) { return a.range_nm != null && a.bearing_deg != null; });
    var max = scopeRange(aircraft);
    if (ring) {
      ring.textContent = hasAircraft(body)
        ? (Math.round(max / 3) + " · " + Math.round(2 * max / 3) + " · " + max + " nm")
        : "—";
    }

    if (!hasAircraft(body)) {
      if (empty) { empty.hidden = false; empty.textContent = "listening… no aircraft overhead yet"; }
      return;
    }
    located.forEach(function (a) { drawDot(group, plot(a.range_nm, a.bearing_deg, max), planeLabel(a)); });
    if (empty) {
      if (!body.receiver_located) {
        // Contacts are being heard but the scope has no location to place them against; say so
        // rather than drop a dot at a guessed point — a placed contact would be a real-looking lie.
        empty.hidden = false;
        empty.textContent = "receiver location not set — set SIGINT101_RX_LAT/LON to place contacts";
      } else if (!located.length) {
        empty.hidden = false;
        empty.textContent = "contacts heard, waiting for their position";
      } else {
        empty.hidden = true;
      }
    }
  }

  function showEmpty(panel, reason) {
    var group = panel.querySelector("[data-adsb-dots]");
    var empty = panel.querySelector("[data-adsb-empty]");
    var badge = panel.querySelector("[data-adsb-badge]");
    var count = panel.querySelector("[data-adsb-count]");
    clear(group);
    if (count) count.textContent = "0";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode ADS-B"; }
  }

  function tick() {
    fetch("/api/adsb", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode ADS-B";
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
