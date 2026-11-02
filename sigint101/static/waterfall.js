// ABOUTME: Draws the live waterfall on the console by polling /api/spectrum for real sweeps.
// ABOUTME: One pixel per rtl_power bin; on 503 it shows the reason and never invents a signal.

(function () {
  "use strict";

  // The gain ladder: automatic (null), then a spread of the R82xx tuner's own gain table, so every
  // rung is a setting the tuner really has and rtl_power applies it as asked. Nine rungs keep
  // "from the bottom up" to a handful of taps. Assigned up here, before the node export below
  // returns, because nextGain reads it under test too.
  var GAIN_STEPS = [0, 7.7, 14.4, 20.7, 28.0, 33.8, 40.2, 44.5, 49.6];

  // Under node's test runner the file is loaded for its pure scalers — the dB→colour map and the
  // unit formatters — and there is no document to draw on. Export them and stop before any canvas
  // access. In the browser there is no `module`, so this is skipped and the panel runs. colour,
  // formatMHz and formatDb are function declarations, hoisted to the top of this IIFE, so they
  // resolve here even though they are written below beside the drawing code that uses them.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      colour: colour, formatMHz: formatMHz, formatDb: formatDb,
      gainMeterGeometry: gainMeterGeometry, parseTuneBand: parseTuneBand,
      rowBounds: rowBounds, binColour: binColour, parseRamp: parseRamp, rampColour: rampColour,
      tapToHz: tapToHz, spanAround: spanAround, formatTuneMHz: formatTuneMHz,
      makeTuner: makeTuner, nextGain: nextGain, formatGain: formatGain,
      makeGainStepper: makeGainStepper, meterReading: meterReading,
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

  // Gathers taps on a control into one request to the console at a time. Taps add up steps, or set
  // an absolute value instead. After `delayMs` without a tap the plan is sent; taps that land while
  // a request is in flight go out after it settles. Each retune or gain change restarts rtl_power,
  // which takes the dongle a second or two to reopen, so a burst of taps must cost one restart.
  //   opts.plan(steps, absolute) -> {value} to send, or null when there is nothing to send
  //                                 (absolute is {value} after a set(), else null)
  //   opts.send(value)           -> Promise of the console's answer
  //   opts.onPending(plan)       -> the plan being gathered, or null once it has settled
  //   opts.onResult(answer)      -> the console's answer
  function makeCoalescer(opts) {
    var steps = 0;
    var absolute = null;
    var timer = null;
    var busy = false;

    function arm() {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(flush, opts.delayMs);
      opts.onPending(opts.plan(steps, absolute));
    }

    function flush() {
      timer = null;
      if (busy) return;               // sent again once the request in flight settles
      var next = opts.plan(steps, absolute);
      steps = 0;
      absolute = null;
      if (!next) { opts.onPending(null); return; }
      busy = true;
      opts.send(next.value).then(function (res) {
        busy = false;
        opts.onResult(res);
        if (steps !== 0 || absolute !== null) arm();
        else opts.onPending(null);
      });
    }

    return {
      step: function (n) { steps += n; arm(); },
      set: function (value) { absolute = { value: value }; steps = 0; arm(); },
    };
  }

  // The tune strip's coalescer. +/- taps add up steps of `stepHz`; a tap on the waterfall sets an
  // absolute frequency. Steps always count from the span the server last confirmed, so the dial
  // never drifts from what the radio is doing; a retune keeps the span's width and bin.
  //   opts.confirmed()   -> {low, high, binSize} the server confirmed, or null (nothing to tune)
  //   opts.send(request) -> Promise of {ok, low, high, bin_size} or {ok: false, status, reason}
  //   opts.onPending(hz) -> the target being gathered, or null once it has settled
  //   opts.onResult(res) -> the server's answer
  function makeTuner(opts) {
    var coalescer = makeCoalescer({
      delayMs: opts.delayMs,
      plan: function (steps, absolute) {
        var span = opts.confirmed();
        if (!span) return null;
        var base = absolute ? absolute.value : (span.low + span.high) / 2;
        return { value: Math.round(base + steps * opts.stepHz) };
      },
      send: function (hz) {
        var span = opts.confirmed();
        var request = spanAround(hz, span.high - span.low);
        if (span.binSize) request.bin_size = span.binSize;
        return opts.send(request);
      },
      onPending: function (plan) { opts.onPending(plan ? plan.value : null); },
      onResult: opts.onResult,
    });
    return { step: coalescer.step, tapTo: coalescer.set };
  }

  // The next rung up (dir 1) or down (dir -1) from a gain. Below the bottom rung is automatic; the
  // top holds. A gain set off the ladder steps to the nearest rung in that direction.
  function nextGain(gain, dir) {
    if (gain === null || gain === undefined) return dir > 0 ? GAIN_STEPS[0] : null;
    if (dir > 0) {
      for (var i = 0; i < GAIN_STEPS.length; i++) if (GAIN_STEPS[i] > gain) return GAIN_STEPS[i];
      return GAIN_STEPS[GAIN_STEPS.length - 1];
    }
    for (var j = GAIN_STEPS.length - 1; j >= 0; j--) if (GAIN_STEPS[j] < gain) return GAIN_STEPS[j];
    return null;
  }

  function formatGain(gain) {
    return gain === null || gain === undefined ? "AUTO" : gain.toFixed(1) + " dB";
  }

  // The gain panel's coalescer: taps walk the ladder from the gain the server confirmed.
  //   opts.confirmed() -> {gain} (null gain is automatic), or null when there is no radio
  //   opts.send(gain)  -> Promise of {set, gain} or {set: false, reason}
  function makeGainStepper(opts) {
    var coalescer = makeCoalescer({
      delayMs: opts.delayMs,
      plan: function (steps) {
        var state = opts.confirmed();
        if (!state || !steps) return null;
        var gain = state.gain;
        for (var i = 0; i < Math.abs(steps); i++) gain = nextGain(gain, steps);
        return gain === state.gain ? null : { value: gain };
      },
      send: opts.send,
      onPending: opts.onPending,
      onResult: opts.onResult,
    });
    return { step: coalescer.step };
  }

  // What the gain meter shows: the sweep's own median floor and strongest bin, as the console
  // computed them, eased from the last reading so it does not flicker. Never the row's minimum,
  // which is the hop-edge roll-off rather than the floor.
  function meterReading(prev, row) {
    if (!prev) return { floor: row.floor_db, peak: row.peak_db };
    return { floor: prev.floor * 0.8 + row.floor_db * 0.2, peak: prev.peak * 0.8 + row.peak_db * 0.2 };
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
  // shows the gap rather than borrow a neighbour's value for a frequency nobody measured. A theme
  // with its own ramp (red-night) paints from it; otherwise the conventional map.
  function binColour(v, floor, range, ramp, emptyColour) {
    if (v === null || v === undefined) return emptyColour;
    var t = (v - floor) / range;
    return ramp ? rampColour(t, ramp) : colour(t);
  }

  // A theme's waterfall ramp, from its --wf-ramp token: two or more #rrggbb colours, floor first.
  // Anything else is null, and the theme keeps the conventional map.
  function parseRamp(token) {
    var parts = String(token || "").trim().split(/\s+/);
    if (parts.length < 2) return null;
    var stops = [];
    for (var i = 0; i < parts.length; i++) {
      var m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(parts[i]);
      if (!m) return null;
      stops.push([parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)]);
    }
    return stops;
  }

  // A level (0 floor .. 1 peak) along a ramp, straight-line between neighbouring stops.
  function rampColour(t, stops) {
    t = Math.max(0, Math.min(1, t));
    var pos = t * (stops.length - 1);
    var i = Math.min(Math.floor(pos), stops.length - 2);
    var f = pos - i;
    var a = stops[i], b = stops[i + 1];
    return "rgb(" + Math.round(a[0] + (b[0] - a[0]) * f) + "," +
                    Math.round(a[1] + (b[1] - a[1]) * f) + "," +
                    Math.round(a[2] + (b[2] - a[2]) * f) + ")";
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
      floorDbm: null, ceilDbm: null, reading: null, sized: false
    });
  });
  if (!panels.length) return;

  // The theme's empty-band colour, read live so a theme switch repaints in the new palette.
  function emptyColour(p) {
    return getComputedStyle(p.canvas).getPropertyValue("--wf-bg").trim() || WF_BG;
  }

  // The theme's own level ramp (red-night's red-only one), or null for the conventional map.
  function themeRamp(p) {
    return parseRamp(getComputedStyle(p.canvas).getPropertyValue("--wf-ramp"));
  }

  // The reason the band is empty, as page text over the canvas (null hides it).
  function showReason(p, message) {
    if (!p.reason) return;
    p.reason.firstElementChild.textContent = message || "";
    p.reason.hidden = !message;
  }

  // A stage that declares the gain panel (M2) carries a vertical level meter beside its waterfall.
  // It reads the sweep's own median floor and strongest bin (the console's floor_db / peak_db), so
  // the floor and the peak lines and the SNR gap shaded between them are the numbers the console
  // computed — not the colour scale's minimum, which sits on the hop-edge roll-off.
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
    if (!m || !p.reading) return;
    // 14 dB of headroom above the peak leaves room for the PEAK label, which sits above its line.
    var g = gainMeterGeometry(p.reading.floor, p.reading.peak, 60, 14);
    if (m.empty) m.empty.hidden = true;
    [m.peak, m.floor, m.gap].forEach(function (el) { if (el) el.hidden = false; });
    if (m.peak) m.peak.style.top = g.peakTop + "%";
    if (m.floor) m.floor.style.top = g.floorTop + "%";
    if (m.gap) { m.gap.style.top = g.gapTop + "%"; m.gap.style.height = g.gapHeight + "%"; }
    if (m.peakV) m.peakV.textContent = formatDb(p.reading.peak);
    if (m.floorV) m.floorV.textContent = formatDb(p.reading.floor);
    if (m.gapV) m.gapV.textContent = formatDb(g.gapDb);
  }

  function clearGainMeter(p) {
    var m = p.meter;
    p.reading = null;
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

  // ---- Gain: the M2 panel's -/+ really change the sweep's gain through /api/gain -------------
  var liveGain = null;      // the gain the server last reported: {gain} (null gain = AUTO), or null
  var gainValues = Array.prototype.slice.call(document.querySelectorAll("[data-gain-value]"));

  function renderGain(pendingPlan, note) {
    gainValues.forEach(function (el) {
      if (pendingPlan) el.textContent = formatGain(pendingPlan.value);
      else el.innerHTML = liveGain ? formatGain(liveGain.gain) : "&mdash;";
      el.classList.toggle("gain-meter__value--pending", !!pendingPlan);
      el.title = note || "";
    });
  }

  function postGain(gain) {
    return fetch("/api/gain", {
      method: "POST", cache: "no-store",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ gain: gain }),
    }).then(function (res) { return res.json(); })
      .catch(function (err) { return { set: false, reason: "lost contact with the console: " + err.message }; });
  }

  var gainStepper = makeGainStepper({
    delayMs: TUNE_DELAY_MS,
    confirmed: function () { return liveGain; },
    send: postGain,
    onPending: function (plan) { renderGain(plan, null); },
    onResult: function (res) {
      if (res.set) liveGain = { gain: res.gain };
      renderGain(null, res.set ? null : res.reason);
    },
  });

  document.querySelectorAll("[data-gain-step]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      gainStepper.step(parseInt(btn.getAttribute("data-gain-step"), 10) || 0);
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
    var ramp = themeRamp(p);
    p.ctx.drawImage(p.canvas, 0, ROW_H);
    for (var i = 0; i < bins; i++) {
      p.ctx.fillStyle = binColour(row.dbm[i], p.floorDbm, range, ramp, empty);
      p.ctx.fillRect(i, 0, 1, ROW_H);
    }
    if (p.meter) {
      p.reading = meterReading(p.reading, row);
      updateGainMeter(p);
    }
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
      p.reading = null;
      p.sized = false;
      if (p.axis) p.axis.innerHTML = "";
      p.ctx.fillStyle = emptyColour(p);
      p.ctx.fillRect(0, 0, p.canvas.width, p.canvas.height);
    });
  }

  // A theme switch repaints the history in the new palette: the canvas is cleared and the painted
  // timestamps forgotten, so the next poll's replay of recent rows draws them again. The axis and
  // the span stay, since the band has not changed. Without this, switching to red-night at night
  // would leave the old rainbow rows on screen until they scrolled away.
  function repaintForTheme() {
    seen = [];
    total = 0;
    panels.forEach(function (p) {
      p.floorDbm = null;
      p.ceilDbm = null;
      p.ctx.fillStyle = emptyColour(p);
      p.ctx.fillRect(0, 0, p.canvas.width, p.canvas.height);
    });
  }
  var appRoot = document.querySelector(".app");
  if (appRoot && window.MutationObserver) {
    new MutationObserver(repaintForTheme).observe(appRoot,
      { attributes: true, attributeFilter: ["data-theme"] });
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
            liveGain = null;
            radioNote = "no radio · not tuned";
            renderDials();
            renderCaptions();
            renderGain(null, null);
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
        liveGain = { gain: result.body.gain === undefined ? null : result.body.gain };
        radioNote = null;
        renderDials();
        renderCaptions();
        if (gainValues.length && !gainValues[0].classList.contains("gain-meter__value--pending")) {
          renderGain(null, null);
        }
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
