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
    module.exports = {
      colour: colour, formatMHz: formatMHz, formatDb: formatDb,
      gainMeterGeometry: gainMeterGeometry, parseTuneBand: parseTuneBand,
      rowBounds: rowBounds, binColour: binColour,
      tapToHz: tapToHz, spanAround: spanAround, formatTuneMHz: formatTuneMHz,
      makeTuner: makeTuner,
    };
    return;
  }

  // The frequency of the bin under a tap. `frac` is how far across the canvas the tap landed
  // (0..1); the canvas holds one column per bin, so the column under the finger is the bin, and
  // its frequency is that bin's centre in the live row. Before any row is drawn (bins 0) the tap
  // maps straight across the span's edges. Either way it is the live span, never the dial +/- a
  // fixed width.
  function tapToHz(frac, low, high, bins) {
    frac = Math.max(0, Math.min(1, frac));
    if (!bins) return Math.round(low + frac * (high - low));
    var index = Math.min(bins - 1, Math.floor(frac * bins));
    return Math.round(low + index * (high - low) / bins);
  }

  // A span of the same width, centred on a new frequency: a retune moves the window, not its size.
  function spanAround(centreHz, widthHz) {
    var low = Math.round(centreHz - widthHz / 2);
    return { low: low, high: low + Math.round(widthHz) };
  }

  function formatTuneMHz(hz) {
    return (hz / 1e6).toFixed(3);
  }

  // Gathers tune taps into one /api/tune request at a time. +/- taps add up steps; a tap on the
  // waterfall sets an absolute frequency instead. After `delayMs` without a tap the target is sent;
  // taps that land while a request is in flight go out after it settles. Steps always count from
  // the span the server last confirmed, so the dial never drifts from what the radio is doing.
  //   opts.confirmed()   -> {low, high, binSize} the server confirmed, or null (nothing to tune)
  //   opts.send(request) -> Promise of {ok, low, high, bin_size} or {ok: false, status, reason}
  //   opts.onPending(hz) -> the target being gathered, or null once it has settled
  //   opts.onResult(res) -> the server's answer
  function makeTuner(opts) {
    var steps = 0;
    var centre = null;
    var timer = null;
    var busy = false;

    function target() {
      var span = opts.confirmed();
      if (!span) return null;
      var base = centre !== null ? centre : (span.low + span.high) / 2;
      return Math.round(base + steps * opts.stepHz);
    }

    function arm() {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(flush, opts.delayMs);
      opts.onPending(target());
    }

    function flush() {
      timer = null;
      if (busy) return;               // sent again once the request in flight settles
      var span = opts.confirmed();
      var hz = target();
      steps = 0;
      centre = null;
      if (!span || hz === null) { opts.onPending(null); return; }
      var request = spanAround(hz, span.high - span.low);
      if (span.binSize) request.bin_size = span.binSize;
      busy = true;
      opts.send(request).then(function (res) {
        busy = false;
        opts.onResult(res);
        if (steps !== 0 || centre !== null) arm();
        else opts.onPending(null);
      });
    }

    return {
      step: function (n) { steps += n; arm(); },
      tapTo: function (hz) { centre = hz; steps = 0; arm(); },
    };
  }

  // The colour-scale bounds of one row: its weakest and strongest measured bins, or null when it
  // has none. A bin rtl_power did not measure arrives as null and is skipped — Math.min would read
  // it as 0 dB and pin the ceiling of every row there.
  function rowBounds(dbm) {
    var lo = Infinity, hi = -Infinity;
    for (var i = 0; i < dbm.length; i++) {
      var v = dbm[i];
      if (v === null || v === undefined) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    return lo === Infinity ? null : { lo: lo, hi: hi };
  }

  // The colour one bin is painted. An unmeasured bin (null) gets the empty-band colour: the panel
  // shows the gap rather than borrow a neighbour's value for a frequency nobody measured.
  function binColour(v, floor, range, ramp, emptyColour) {
    if (v === null || v === undefined) return emptyColour;
    return colour((v - floor) / range);
  }

  // Turn a canvas's data-tune-low/high attribute pair into an /api/tune request, or null when the
  // pair is absent or not two sane integer edges. Pure, so it is tested without a DOM; the attribute
  // read and the POST are the browser-only wiring around it. A null means no tune is sent and the
  // sweep keeps its default band rather than being pointed at a malformed span. A band that names
  // its bin size (data-tune-bin) asks for it; without one the sweep keeps its default bin.
  function parseTuneBand(lowAttr, highAttr, binAttr) {
    var low = parseInt(lowAttr, 10);
    var high = parseInt(highAttr, 10);
    if (!isFinite(low) || !isFinite(high) || low <= 0 || high <= 0 || high <= low) return null;
    var band = { low: low, high: high };
    var bin = parseInt(binAttr, 10);
    if (isFinite(bin) && bin > 0) band.bin_size = bin;
    return band;
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

  // Place a floor/peak dB pair on a fixed-span vertical track, as percentages from the top. The
  // window is pinned a few dB above the peak, so turning the gain up pushes the peak AND the floor
  // up the track together while the gap between them — the SNR, the whole lesson of M2 — stays
  // readable as the height of the shaded band. Pure arithmetic, unit-tested like the colour map.
  function gainMeterGeometry(floorDb, peakDb, span, headroom) {
    span = span || 60;
    headroom = headroom === undefined ? 4 : headroom;
    var hi = peakDb + headroom;
    var pos = function (db) { return Math.max(0, Math.min(100, (hi - db) / span * 100)); };
    var peakTop = pos(peakDb);
    var floorTop = pos(floorDb);
    return {
      peakTop: peakTop,
      floorTop: floorTop,
      gapTop: peakTop,
      gapHeight: Math.max(floorTop - peakTop, 0),
      gapDb: Math.max(peakDb - floorDb, 0),
    };
  }

  // Every instrument stage has its own waterfall; they all show the same live spectrum (there is
  // one radio). Finding them all — rather than a single #id — is what lets a hidden stage keep its
  // history and be already drawn when the learner steps to it. No wraps means no console on this
  // page (e.g. the predict gate, or the home grid), so there is nothing to do.
  var wraps = Array.prototype.slice.call(document.querySelectorAll(".waterfall-wrap"));
  if (!wraps.length) return;

  var WF_BG = "#05080b";   // the empty-band colour if the theme's --wf-bg cannot be read
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
      reason: wrap.querySelector("[data-wf-reason]"),
      meter: buildMeterRefs(wrap),
      floorDbm: null, ceilDbm: null, sized: false
    });
  });
  if (!panels.length) return;

  // The theme's empty-band colour, read live so a theme switch repaints in the new palette.
  function emptyColour(p) {
    return getComputedStyle(p.canvas).getPropertyValue("--wf-bg").trim() || WF_BG;
  }

  // The reason the band is empty, as page text over the canvas (null hides it).
  function showReason(p, message) {
    if (!p.reason) return;
    p.reason.firstElementChild.textContent = message || "";
    p.reason.hidden = !message;
  }

  // A stage that declares the gain panel (M2) carries a vertical level meter beside its waterfall.
  // It is driven from the same smoothed floor/ceiling the colour scale already tracks, so the one
  // live sweep feeds both: the floor and the peak lines, and the SNR gap shaded between them.
  function buildMeterRefs(wrap) {
    var root = wrap.closest(".instrument");
    var meter = root && root.querySelector("[data-gain-meter]");
    if (!meter) return null;
    return {
      empty: meter.querySelector("[data-gain-empty]"),
      peak: meter.querySelector("[data-gain-peak]"),
      floor: meter.querySelector("[data-gain-floor]"),
      gap: meter.querySelector("[data-gain-gap]"),
      peakV: meter.querySelector("[data-gain-peak-v]"),
      floorV: meter.querySelector("[data-gain-floor-v]"),
      gapV: meter.querySelector("[data-gain-gap-v]"),
    };
  }

  function updateGainMeter(p) {
    var m = p.meter;
    if (!m || p.floorDbm === null || p.ceilDbm === null) return;
    var g = gainMeterGeometry(p.floorDbm, p.ceilDbm);
    if (m.empty) m.empty.hidden = true;
    [m.peak, m.floor, m.gap].forEach(function (el) { if (el) el.hidden = false; });
    if (m.peak) m.peak.style.top = g.peakTop + "%";
    if (m.floor) m.floor.style.top = g.floorTop + "%";
    if (m.gap) { m.gap.style.top = g.gapTop + "%"; m.gap.style.height = g.gapHeight + "%"; }
    if (m.peakV) m.peakV.textContent = formatDb(p.ceilDbm);
    if (m.floorV) m.floorV.textContent = formatDb(p.floorDbm);
    if (m.gapV) m.gapV.textContent = formatDb(g.gapDb);
  }

  function clearGainMeter(p) {
    var m = p.meter;
    if (!m) return;
    if (m.empty) m.empty.hidden = false;
    [m.peak, m.floor, m.gap].forEach(function (el) { if (el) el.hidden = true; });
  }

  var seen = [];          // timestamps already painted, newest last
  var total = 0;          // sweeps painted since the last retune; `seen` is trimmed
  var waiting = false;    // radio unavailable; polling slowly until it returns
  var stopped = false;    // only ever set when the page is going away
  var span = null;        // "low:high" of the span currently on screen
  var live = null;        // the span the server last confirmed: {low, high, binSize}, or null
  var lastRow = null;     // the newest painted row's own geometry: {low, high, bins}

  // ---- Tuning: +/- on the tune strip and a tap on the waterfall retune the live sweep ----------
  // Every instrument stage has its own strip; they drive the one radio together, so one tuner
  // serves them all and every dial shows the same confirmed frequency.
  var TUNE_DELAY_MS = 450;  // taps closer together than this become one retune
  var strips = Array.prototype.slice.call(document.querySelectorAll("[data-tune-strip]"))
    .map(function (el) {
      var caption = el.querySelector("[data-tune-caption]");
      return {
        el: el, freq: el.querySelector("[data-freq]"), caption: caption,
        base: caption ? caption.textContent : "",
      };
    });
  var stepHz = strips.length ? (parseInt(strips[0].el.getAttribute("data-step-hz"), 10) || 100000)
                             : 100000;
  var pendingNote = null;   // "tuning to …" while taps are gathered or a request is in flight
  var tuneNote = null;      // why the last retune was refused, until the next tap
  var radioNote = null;     // why nothing is tuned (no radio), until the radio answers

  function renderCaptions() {
    var text = pendingNote || tuneNote || radioNote;
    strips.forEach(function (s) {
      if (!s.caption) return;
      s.caption.textContent = text || s.base;
      s.caption.title = text || "";
    });
  }

  // The dial shows the centre of the span the server confirmed. Until there is one it keeps the
  // lesson's target, marked pending, and carries no data-tuned-hz — that attribute is what the
  // ppm panel measures at, so it exists only for a frequency the radio is really on.
  function renderDials() {
    strips.forEach(function (s) {
      if (!s.freq) return;
      if (live) {
        var hz = Math.round((live.low + live.high) / 2);
        s.freq.innerHTML = formatTuneMHz(hz) + "<span>MHz</span>";
        s.freq.setAttribute("data-tuned-hz", String(hz));
        s.freq.classList.remove("tune__freq--pending");
      } else {
        s.freq.removeAttribute("data-tuned-hz");
        s.freq.classList.add("tune__freq--pending");
      }
    });
  }

  function postTune(request) {
    return fetch("/api/tune", {
      method: "POST", cache: "no-store",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request),
    }).then(function (res) {
      return res.json().then(function (body) {
        body.ok = res.ok;
        body.status = res.status;
        return body;
      });
    }).catch(function (err) {
      return { ok: false, status: 0, reason: "lost contact with the console: " + err.message };
    });
  }

  var tuner = makeTuner({
    stepHz: stepHz,
    delayMs: TUNE_DELAY_MS,
    confirmed: function () { return live; },
    send: postTune,
    onPending: function (hz) {
      pendingNote = hz === null ? null : "tuning to " + formatTuneMHz(hz) + " MHz…";
      renderCaptions();
    },
    onResult: function (res) {
      if (res.ok) {
        live = { low: res.low, high: res.high, binSize: res.bin_size };
        tuneNote = null;
        renderDials();
      } else {
        tuneNote = "not tuned: " + (res.reason || "refused");
      }
      renderCaptions();
    },
  });

  strips.forEach(function (s) {
    s.el.querySelectorAll("[data-tune-step]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        tuneNote = null;
        tuner.step(parseInt(btn.getAttribute("data-tune-step"), 10) || 0);
      });
    });
  });

  // A tap on the waterfall tunes to the bin under the finger, read off the newest painted row's
  // own geometry (or the span's edges before the first row lands). No confirmed span, no tune.
  panels.forEach(function (p) {
    p.canvas.addEventListener("click", function (ev) {
      if (!live) return;
      var rect = p.canvas.getBoundingClientRect();
      var geom = lastRow || { low: live.low, high: live.high, bins: 0 };
      tuneNote = null;
      tuner.tapTo(tapToHz((ev.clientX - rect.left) / rect.width, geom.low, geom.high, geom.bins));
    });
  });

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
    var empty = emptyColour(p);
    if (!p.sized || p.canvas.width !== bins) {
      p.canvas.width = bins;
      p.sized = true;
      p.ctx.fillStyle = empty;
      p.ctx.fillRect(0, 0, p.canvas.width, p.canvas.height);
    }

    var bounds = rowBounds(row.dbm);
    if (!bounds) return;
    // Ease the bounds so the colour scale doesn't flicker between sweeps.
    p.floorDbm = p.floorDbm === null ? bounds.lo : p.floorDbm * 0.8 + bounds.lo * 0.2;
    p.ceilDbm = p.ceilDbm === null ? bounds.hi : p.ceilDbm * 0.8 + bounds.hi * 0.2;
    var range = Math.max(p.ceilDbm - p.floorDbm, 1);

    // Scroll down by one band, then paint the newest sweep across the top.
    p.ctx.drawImage(p.canvas, 0, ROW_H);
    for (var i = 0; i < bins; i++) {
      p.ctx.fillStyle = binColour(row.dbm[i], p.floorDbm, range, null, empty);
      p.ctx.fillRect(i, 0, 1, ROW_H);
    }
    updateGainMeter(p);
  }

  // The honest empty state: the band is drawn at its empty colour and the reason is shown over it
  // as page text. No carriers, no invented signal — just why there is nothing to show.
  function showEmpty(p, message) {
    p.sized = false;
    p.ctx.fillStyle = emptyColour(p);
    p.ctx.fillRect(0, 0, p.canvas.width, p.canvas.height);
    showReason(p, message);
    clearGainMeter(p);
  }

  function resetView() {
    // A row carries its own frequency axis. Rows from the previous span would be painted against
    // the new one, so the canvas starts clean after every retune.
    seen = [];
    total = 0;
    lastRow = null;
    panels.forEach(function (p) {
      p.floorDbm = null;
      p.ceilDbm = null;
      p.sized = false;
      if (p.axis) p.axis.innerHTML = "";
      p.ctx.fillStyle = emptyColour(p);
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
            live = null;
            radioNote = "no radio · not tuned";
            renderDials();
            renderCaptions();
            say("no radio", "na", reason);
            panels.forEach(function (p) { showEmpty(p, reason); });
          }
          return;
        }
        waiting = false;
        panels.forEach(function (p) { showReason(p, null); });
        // The span the sweep is really on, as the server reports it: the dial reads this, and a
        // +/- step counts from it.
        live = { low: result.body.freq_low, high: result.body.freq_high,
                 binSize: result.body.bin_size };
        radioNote = null;
        renderDials();
        renderCaptions();
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
          lastRow = { low: row.freq_low, high: row.freq_high, bins: row.dbm.length };
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

  // A lesson that names its own band (data-tune-low/high on the canvas) points the shared sweep at
  // it once on load, so the waterfall shows that band instead of the default FM sweep. The retune
  // is detected on the next poll like any other span change. Polling starts whatever the outcome —
  // on no radio the tune 503s and the panel still shows the honest no-radio state.
  function autoTuneToBand() {
    for (var i = 0; i < panels.length; i++) {
      var c = panels[i].canvas;
      var band = parseTuneBand(c.getAttribute("data-tune-low"), c.getAttribute("data-tune-high"),
                               c.getAttribute("data-tune-bin"));
      // A refusal or no radio needs no handling here: the next poll draws the band honestly.
      if (band) return postTune(band);
    }
    return Promise.resolve();
  }

  say("connecting", "sample", "starting the sweep");
  autoTuneToBand().then(tick);
})();
