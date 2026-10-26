// ABOUTME: Draws the live waterfall on the console by polling /api/spectrum for real sweeps.
// ABOUTME: One pixel per rtl_power bin; on 503 it shows the reason and never invents a signal.

(function () {
  "use strict";

  // Under node's test runner the file is loaded for its pure scalers — the dB→colour map and the
  // unit formatters — and there is no document to draw on. Export them and stop before any canvas
  // access. In the browser there is no `module`, so this is skipped and the panel runs. colour,
  // formatMHz and formatDb are function declarations, hoisted to the top of this IIFE, so they
  // resolve here even though they are written below beside the drawing code that uses them.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { colour: colour, formatMHz: formatMHz, formatDb: formatDb };
    return;
  }

  // Blue -> cyan -> green -> yellow -> red. Conventional for RF, and legible on dark.
  function colour(t) {
    t = Math.max(0, Math.min(1, t));
    var r, g, b;
    if (t < 0.25)      { r = 0;                    g = Math.round(t * 4 * 190);        b = Math.round(60 + t * 4 * 130); }
    else if (t < 0.5)  { r = 0;                    g = Math.round(190 + (t - 0.25) * 4 * 40); b = Math.round(190 - (t - 0.25) * 4 * 150); }
    else if (t < 0.75) { r = Math.round((t - 0.5) * 4 * 245); g = 230;                  b = 40; }
    else               { r = 245;                  g = Math.round(230 - (t - 0.75) * 4 * 200); b = Math.round(40 - (t - 0.75) * 4 * 40); }
    return "rgb(" + r + "," + g + "," + b + ")";
  }

  function formatMHz(hz) {
    return (hz / 1e6).toFixed(1) + " MHz";
  }

  function formatDb(db) {
    return db.toFixed(1) + " dB";
  }

  // Every instrument stage has its own waterfall; they all show the same live spectrum (there is
  // one radio). Finding them all — rather than a single #id — is what lets a hidden stage keep its
  // history and be already drawn when the learner steps to it. No wraps means no console on this
  // page (e.g. the predict gate, or the home grid), so there is nothing to do.
  var wraps = Array.prototype.slice.call(document.querySelectorAll(".waterfall-wrap"));
  if (!wraps.length) return;

  var WF_BG = "#05080b";   // matches --wf-bg; the empty-band colour
  var POLL_MS = 900;       // rtl_power reports ~once a second
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var ROW_H = 4;           // one sweep gets a 4px band, so 1 px/s motion stays legible

  // One renderer per canvas: its own 2d context and smoothed colour-scale bounds, so each stage's
  // waterfall scrolls independently and correctly.
  var panels = [];
  wraps.forEach(function (wrap) {
    var canvas = wrap.querySelector("canvas.waterfall");
    if (!canvas) return;
    panels.push({
      canvas: canvas,
      ctx: canvas.getContext("2d", { willReadFrequently: true }),
      badge: wrap.querySelector(".badge"),
      axis: wrap.querySelector(".waterfall__axis"),
      floorDbm: null, ceilDbm: null, sized: false
    });
  });
  if (!panels.length) return;

  var seen = [];          // timestamps already painted, newest last
  var total = 0;          // sweeps painted since the last retune; `seen` is trimmed
  var waiting = false;    // radio unavailable; polling slowly until it returns
  var stopped = false;    // only ever set when the page is going away
  var span = null;        // "low:high" of the span currently on screen

  // The badge is the per-wrap label. It is born as the honest SAMPLE placeholder; the live path
  // replaces it with the real state (live / no radio), so a learner is never shown a sample that
  // pretends to be live, nor a blank panel that hides a dead radio. The full reason rides in the
  // title attribute, since the badge itself stays short.
  function say(text, kind, title) {
    panels.forEach(function (p) {
      if (!p.badge) return;
      p.badge.textContent = text;
      p.badge.className = "badge badge--" + (kind || "sample");
      p.badge.title = title || "";
    });
  }

  // Three labels — low edge, centre, high edge — because on a phone-sized kiosk more ticks
  // overlap. Rebuilt only when the span changes, not on every poll.
  function drawAxis(p, body) {
    if (!p.axis) return;
    var mid = (body.freq_low + body.freq_high) / 2;
    p.axis.innerHTML = "";
    [body.freq_low, mid, body.freq_high].forEach(function (hz) {
      var el = document.createElement("span");
      el.textContent = formatMHz(hz);
      p.axis.appendChild(el);
    });
  }

  function drawRow(p, row) {
    var bins = row.dbm.length;
    if (!bins) return;

    // One canvas pixel per rtl_power bin, so no interpolation can invent signal that was never
    // measured. Resizing wipes the canvas — that is the browser, not us.
    if (!p.sized || p.canvas.width !== bins) {
      p.canvas.width = bins;
      p.sized = true;
      p.ctx.fillStyle = WF_BG;
      p.ctx.fillRect(0, 0, p.canvas.width, p.canvas.height);
    }

    var lo = Math.min.apply(null, row.dbm);
    var hi = Math.max.apply(null, row.dbm);
    // Ease the bounds so the colour scale doesn't flicker between sweeps.
    p.floorDbm = p.floorDbm === null ? lo : p.floorDbm * 0.8 + lo * 0.2;
    p.ceilDbm = p.ceilDbm === null ? hi : p.ceilDbm * 0.8 + hi * 0.2;
    var range = Math.max(p.ceilDbm - p.floorDbm, 1);

    // Scroll down by one band, then paint the newest sweep across the top.
    p.ctx.drawImage(p.canvas, 0, ROW_H);
    for (var i = 0; i < bins; i++) {
      p.ctx.fillStyle = colour((row.dbm[i] - p.floorDbm) / range);
      p.ctx.fillRect(i, 0, 1, ROW_H);
    }
  }

  // The honest empty state: the band is drawn at its noise colour and the reason is written over
  // it. No carriers, no invented signal — just why there is nothing to show.
  function showEmpty(p, message) {
    p.sized = false;
    p.ctx.fillStyle = WF_BG;
    p.ctx.fillRect(0, 0, p.canvas.width, p.canvas.height);
    if (message) {
      p.ctx.fillStyle = "#9fb0bf";
      p.ctx.font = "13px ui-monospace, monospace";
      p.ctx.textAlign = "center";
      p.ctx.fillText(message, p.canvas.width / 2, p.canvas.height / 2);
      p.ctx.textAlign = "left";
    }
  }

  function resetView() {
    // A row carries its own frequency axis. Rows from the previous span would be painted against
    // the new one, so the canvas starts clean after every retune.
    seen = [];
    total = 0;
    panels.forEach(function (p) {
      p.floorDbm = null;
      p.ceilDbm = null;
      p.sized = false;
      if (p.axis) p.axis.innerHTML = "";
      p.ctx.fillStyle = WF_BG;
      p.ctx.fillRect(0, 0, p.canvas.width, p.canvas.height);
    });
  }

  // Timestamps of sweeps already painted. Trimmed rather than unbounded because the kiosk runs for
  // days; only recent ones matter since the API replays at most 40 rows.
  function remember(ts) {
    seen.push(ts);
    total += 1;
    if (seen.length > 400) seen = seen.slice(-200);
  }

  function tick() {
    if (stopped) return;
    fetch("/api/spectrum?limit=40", { cache: "no-store" })
      .then(function (res) {
        return res.json().then(function (body) { return { code: res.status, body: body }; });
      })
      .then(function (result) {
        if (result.code === 503) {
          // An honest refusal, reported verbatim — but keep checking, because the radio usually
          // comes back (another program borrows the dongle and hands it back). The reason is shown
          // once on the transition rather than redrawn each retry.
          var reason = (result.body && result.body.reason) || "the radio is unavailable";
          if (!waiting) {
            waiting = true;
            span = null;
            say("no radio", "na", reason);
            panels.forEach(function (p) { showEmpty(p, reason); });
          }
          return;
        }
        waiting = false;
        // A retune (from this tab or any other) changes what the x-axis means, so rows drawn for
        // the old span would be lies. Detect it and start clean; redraw the axis for the new span.
        var key = result.body.freq_low + ":" + result.body.freq_high;
        var spanChanged = (span === null) || (key !== span);
        if (span !== null && key !== span) resetView();
        span = key;
        if (spanChanged) panels.forEach(function (p) { drawAxis(p, result.body); });

        // The API replays its recent sweeps on every poll; `seen` keeps a row from being painted
        // twice, which would scroll the waterfall faster than time passes.
        var rows = result.body.rows || [];
        var fresh = rows.filter(function (row) { return seen.indexOf(row.ts) === -1; });
        fresh.forEach(function (row) {
          remember(row.ts);
          panels.forEach(function (p) { drawRow(p, row); });
        });

        if (!total) {
          say("waiting", "sample",
              "sweeping " + formatMHz(result.body.freq_low) + "–" +
              formatMHz(result.body.freq_high) + ", waiting for the first pass");
        } else {
          say("live · " + total, "live", "live sweep");
        }
      })
      .catch(function (err) {
        say("no console", "na", "lost contact with the console: " + err.message);
      })
      .then(function () {
        // Poll gently while the radio is away — another program holds the dongle, and hammering
        // the API will not bring it back any sooner.
        if (!stopped) setTimeout(tick, waiting ? RETRY_MS : POLL_MS);
      });
  }

  say("connecting", "sample", "starting the sweep");
  tick();
})();
