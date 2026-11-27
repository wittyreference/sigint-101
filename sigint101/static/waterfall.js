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
      tapToHz: tapToHz, bandRequest: bandRequest, formatTuneMHz: formatTuneMHz,
      makeTuner: makeTuner, nextGain: nextGain, formatGain: formatGain,
      makeGainStepper: makeGainStepper, meterReading: meterReading,
      meterLabelTops: meterLabelTops, sweepBadge: sweepBadge, meterState: meterState,
      restartKey: restartKey, makeRestarter: makeRestarter, restartNote: restartNote,
      meterNoteText: meterNoteText,
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

  // A lesson's band as an /api/tune request: its centre and width (and bin, if it names one). The
  // console builds the span around the centre itself, offset so the target is not lost in a hop's
  // unmeasured centre bin, and answers with the span it chose and the centre the dial reads.
  function bandRequest(band) {
    if (!band) return null;
    var request = { center: Math.round((band.low + band.high) / 2), span: band.high - band.low };
    if (band.bin_size) request.bin_size = band.bin_size;
    return request;
  }

  // Where the dial is: the frequency tuned to, which the console reports as the span's `center`
  // (offset off the span's middle when tuned to a target), or the middle of a span given by edges.
  function dialHz(span) {
    return typeof span.center === "number" ? span.center : (span.low + span.high) / 2;
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
  // absolute frequency. Steps always count from the frequency the server last confirmed, so the
  // dial never drifts from what the radio is doing; a retune keeps the span's width and bin, and
  // asks for the target by centre so the console can offset the sweep off it.
  //   opts.stepHz        -> the step in Hz, or a function read at each tap (the rack's step key)
  //   opts.confirmed()   -> {low, high, binSize, center} the server confirmed, or null
  //   opts.send(request) -> Promise of {ok, low, high, bin_size, center} or {ok: false, ...}
  //   opts.onPending(hz) -> the target being gathered, or null once it has settled
  //   opts.onResult(res) -> the server's answer
  function makeTuner(opts) {
    function stepHz() {
      return typeof opts.stepHz === "function" ? opts.stepHz() : opts.stepHz;
    }
    var coalescer = makeCoalescer({
      delayMs: opts.delayMs,
      plan: function (steps, absolute) {
        var span = opts.confirmed();
        if (!span) return null;
        var base = absolute ? absolute.value : dialHz(span);
        return { value: Math.round(base + steps * stepHz()) };
      },
      send: function (hz) {
        var span = opts.confirmed();
        var request = { center: hz, span: span.high - span.low };
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

  // What the gain meter shows after one poll: a reading, or a note in place of one. While the
  // console's newest row is stale (`stale`: "stalled", or "resuming" for the first two passes after
  // a stall, which carry levels from before it) the meter has no floor or peak to show, and says
  // why. A meter with no reading starts again from the newest fresh row alone: the console's rule
  // covers only its newest row, and one painted before it in the same poll may be stale; with no
  // row to read yet it is waiting. A meter with a reading eases it through each fresh row.
  function meterState(prev, fresh, stale) {
    // While the badge waits for a first pass, the meter keeps its usual between-pass behavior.
    if (stale && stale !== "waiting") return { reading: null, note: stale };
    var reading = prev;
    (prev ? fresh : fresh.slice(-1)).forEach(function (row) {
      reading = meterReading(reading, row);
    });
    return reading ? { reading: reading, note: null } : { reading: null, note: "waiting" };
  }

  // The waterfall's badge, short, with the whole account in its title. A stale newest row reads
  // stalled or resuming by the console's own rule, the one the baseline panel's reading follows;
  // the rows already painted stay, since they are real. Stalled takes the colour of a tool that
  // stopped (a decoder's STOPPED); resuming, like waiting, the colour of one getting going.
  function sweepBadge(body, total) {
    if (body.stale === "stalled") {
      return { text: "stalled", kind: "na",
               title: "no new pass from rtl_power: the rows shown are from before it stalled" };
    }
    if (body.stale === "resuming") {
      return { text: "resuming", kind: "sample",
               title: "the first passes after a stall carry levels from before it" };
    }
    if (body.stale === "restarting") {
      return { text: "restarting", kind: "sample",
               title: "rtl_power was restarted after a stall: the rows shown are from before it" };
    }
    if (body.stale === "waiting" || !total) {
      return { text: "waiting", kind: "sample",
               title: "sweeping " + formatMHz(body.freq_low) + "–" + formatMHz(body.freq_high) +
                      ", waiting for the first pass" };
    }
    return { text: "live · " + total, kind: "live", title: "live sweep" };
  }

  // The meter's note in place of a floor and a peak, or its own empty line with no note. Lesson 2's
  // note box holds 12 characters of its mono face on one line, so "restarting" reads "starting"
  // there ("— restarting —" wrapped onto two), while the badge says RESTARTING.
  function meterNoteText(note, emptyText) {
    if (!note) return emptyText;
    return "— " + (note === "restarting" ? "starting" : note) + " —";
  }

  // The Restart sweep key after a poll: shown only when the console answered and said the sweep has
  // stalled (the owner's ruling), never on a refusal (503: no radio, or the radio handed to another
  // tool) or lost contact (poll null); disabled while a press is in flight.
  //   poll: {code, body} as the last poll answered, or null
  function restartKey(poll, pending) {
    var shown = !!poll && poll.code === 200 && !!poll.body && poll.body.stale === "stalled";
    return { shown: shown, disabled: !!pending };
  }

  // A press on the key: one request at a time. press() sends nothing while one is in flight and says
  // so (false); otherwise it marks the key pending, sends, and hands on the console's answer.
  //   opts.send()        -> Promise of the answer ({ok, status, ...}); never rejects
  //   opts.onPending(p)  -> true when the request goes, false when it is answered
  //   opts.onResult(res) -> the answer
  function makeRestarter(opts) {
    var busy = false;
    return {
      press: function () {
        if (busy) return false;
        busy = true;
        opts.onPending(true);
        opts.send().then(function (res) {
          busy = false;
          opts.onPending(false);
          opts.onResult(res);
        });
        return true;
      },
    };
  }

  // What a press's answer puts on the tune caption: nothing for a restart (200) or a press that
  // found nothing to restart (409), since the next poll shows how the sweep stands; the reason
  // otherwise, a lost console included.
  function restartNote(answer) {
    if (answer.ok || answer.status === 409) return null;
    return "not restarted: " + (answer.reason || "refused");
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

  // Where the meter's three readings sit on its track, as px from the track's top, so they never
  // overlap and never leave it at any SNR. The lines stay at their true dB positions (peakY,
  // floorY); only the labels give way. PEAK sits over its line, FLOOR under its line, the SNR
  // reading centred on the band; a band too thin for the reading pushes PEAK up and FLOOR down,
  // and a stack that runs off either end of the track is slid back onto it.
  //   heights: {peak, snr, floor}, each label's height in px
  function meterLabelTops(trackH, peakY, floorY, heights) {
    var snr = (peakY + floorY) / 2 - heights.snr / 2;
    var peak = Math.min(peakY, snr) - heights.peak;
    var floor = Math.max(floorY, snr + heights.snr);
    if (peak < 0) {
      peak = 0;
      snr = Math.max(snr, peak + heights.peak);
      floor = Math.max(floor, snr + heights.snr);
    }
    if (floor + heights.floor > trackH) {
      floor = trackH - heights.floor;
      snr = Math.min(snr, floor - heights.snr);
      peak = Math.min(peak, snr - heights.peak);
    }
    return { peak: peak, snr: snr, floor: floor };
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
      restart: wrap.querySelector("[data-wf-restart]"),
      meter: buildMeterRefs(wrap),
      floorDbm: null, ceilDbm: null, reading: null, geometry: null, sized: false
    });
  });
  if (!panels.length) return;

  // A meter on a hidden stage has no size to lay its labels out against; once its stage is shown
  // the track takes its height, and the labels are laid out then rather than at the next sweep.
  if (window.ResizeObserver) {
    panels.forEach(function (p) {
      if (p.meter && p.meter.track) {
        new ResizeObserver(function () { placeMeterLabels(p); }).observe(p.meter.track);
      }
    });
  }

  // The theme's empty-band colour, read live so a theme switch repaints in the new palette.
  function emptyColour(p) {
    return getComputedStyle(p.canvas).getPropertyValue("--wf-bg").trim() || WF_BG;
  }

  // The theme's own level ramp (red-night's red-only one), or null for the conventional map.
  function themeRamp(p) {
    return parseRamp(getComputedStyle(p.canvas).getPropertyValue("--wf-ramp"));
  }

  // The sweep's own command line on each stage's command strip (decoder-panel.js), or hidden when
  // no rtl_power runs: the learner sees exactly what the waterfall is drawing from.
  function showCommand(command) {
    if (!window.DecoderPanel) return;
    panels.forEach(function (p) {
      var instrument = p.canvas.closest(".instrument");
      if (instrument) window.DecoderPanel.showCommand(instrument, command);
    });
  }

  // The reason the band is empty, as page text over the canvas (null hides it).
  function showReason(p, down) {
    if (!p.reason) return;
    p.reason.hidden = !down;
    if (down) window.DecoderPanel.renderRefusal(p.reason.firstElementChild, down);
    else p.reason.firstElementChild.textContent = "";
  }

  // A stage that declares the gain panel (M2) carries a vertical level meter beside its waterfall.
  // It reads the sweep's own median floor and strongest bin (the console's floor_db / peak_db), so
  // the floor and the peak lines and the SNR gap shaded between them are the numbers the console
  // computed — not the colour scale's minimum, which sits on the hop-edge roll-off.
  function buildMeterRefs(wrap) {
    var root = wrap.closest(".instrument");
    var meter = root && root.querySelector("[data-gain-meter]");
    if (!meter) return null;
    var empty = meter.querySelector("[data-gain-empty]");
    return {
      empty: empty,
      emptyText: empty ? empty.textContent : "",
      peak: meter.querySelector("[data-gain-peak]"),
      floor: meter.querySelector("[data-gain-floor]"),
      gap: meter.querySelector("[data-gain-gap]"),
      peakV: meter.querySelector("[data-gain-peak-v]"),
      floorV: meter.querySelector("[data-gain-floor-v]"),
      gapV: meter.querySelector("[data-gain-gap-v]"),
      track: meter.querySelector(".gain-meter__track"),
    };
  }

  function updateGainMeter(p) {
    var m = p.meter;
    if (!m || !p.reading) return;
    // 14 dB of headroom above the peak leaves room for the PEAK label, which sits above its line.
    var g = gainMeterGeometry(p.reading.floor, p.reading.peak, 60, 14);
    p.geometry = g;
    if (m.empty) m.empty.hidden = true;
    [m.peak, m.floor, m.gap].forEach(function (el) { if (el) el.hidden = false; });
    if (m.peak) m.peak.style.top = g.peakTop + "%";
    if (m.floor) m.floor.style.top = g.floorTop + "%";
    if (m.gap) { m.gap.style.top = g.gapTop + "%"; m.gap.style.height = g.gapHeight + "%"; }
    if (m.peakV) m.peakV.textContent = formatDb(p.reading.peak);
    if (m.floorV) m.floorV.textContent = formatDb(p.reading.floor);
    if (m.gapV) m.gapV.textContent = formatDb(g.gapDb);
    placeMeterLabels(p);
  }

  // Slide the three readings clear of each other (meterLabelTops). Each label moves off its
  // natural spot through --label-shift, which app.css applies to the label alone, so the peak and
  // floor lines stay at their true dB. A meter with no size yet (a hidden stage) is left for its
  // ResizeObserver.
  function placeMeterLabels(p) {
    var m = p.meter, g = p.geometry;
    if (!m || !g || !m.track || !m.peak || !m.floor || !m.gap) return;
    var trackH = m.track.clientHeight;
    if (!trackH) return;
    var height = function (el) { return el.getBoundingClientRect().height; };
    var h = {
      peak: height(m.peak),
      snr: Math.max.apply(null, Array.prototype.map.call(m.gap.children, height)),
      floor: height(m.floor),
    };
    var tops = meterLabelTops(trackH, g.peakTop / 100 * trackH, g.floorTop / 100 * trackH, h);
    // Each shift is measured from where the label sits unshifted, read off the page: a mark's
    // own box never moves (only its label does), and the SNR reading's spot is where it is now
    // less the shift it already has.
    var trackTop = m.track.getBoundingClientRect().top;
    var topOf = function (el) { return el.getBoundingClientRect().top - trackTop; };
    var snrShift = parseFloat(m.gap.style.getPropertyValue("--label-shift")) || 0;
    var snrNow = Math.min.apply(null, Array.prototype.map.call(m.gap.children, topOf)) - snrShift;
    m.peak.style.setProperty("--label-shift", (tops.peak - topOf(m.peak)) + "px");
    m.gap.style.setProperty("--label-shift", (tops.snr - snrNow) + "px");
    m.floor.style.setProperty("--label-shift", (tops.floor - topOf(m.floor)) + "px");
  }

  // Empty the meter: its own "no radio" line, or the note given (stalled, resuming) in its place.
  function clearGainMeter(p, note) {
    var m = p.meter;
    p.reading = null;
    p.geometry = null;
    if (!m) return;
    if (m.empty) {
      m.empty.textContent = meterNoteText(note, m.emptyText);
      m.empty.hidden = false;
    }
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
  // Read at each tap rather than once: the rack's step key rewrites data-step-hz on the strip.
  function stepHz() {
    return strips.length ? (parseInt(strips[0].el.getAttribute("data-step-hz"), 10) || 100000)
                         : 100000;
  }
  var pendingNote = null;   // "tuning to …" while taps are gathered or a request is in flight
  var tuneNote = null;      // why the last retune was refused, until the next tap
  var radioNote = null;     // short dial caption while the sweep is unavailable
  var restartNoteText = null;   // why a Restart sweep press was refused, until a later poll answers

  function renderCaptions() {
    var text = radioNote || pendingNote || restartNoteText || tuneNote;
    strips.forEach(function (s) {
      if (!s.caption) return;
      s.caption.textContent = text || s.base;
      s.caption.title = text || "";
    });
  }

  // The dial shows the frequency the server confirmed it tuned to (dialHz: the target, which an
  // offset sweep keeps on measured bins). Until there is one it keeps the lesson's target, marked
  // pending, and carries no data-tuned-hz — that attribute is what the ppm panel measures at, so it
  // exists only for a frequency the radio is really on.
  function renderDials() {
    strips.forEach(function (s) {
      if (!s.freq) return;
      if (live) {
        var hz = Math.round(dialHz(live));
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
        live = { low: res.low, high: res.high, binSize: res.bin_size, center: res.center };
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
    if (p.canvas.getAttribute("data-tune") === null) return;
    p.canvas.addEventListener("click", function (ev) {
      if (!live) return;
      var rect = p.canvas.getBoundingClientRect();
      var geom = lastRow || { low: live.low, high: live.high, bins: 0 };
      tuneNote = null;
      tuner.tapTo(tapToHz((ev.clientX - rect.left) / rect.width, geom.low, geom.high, geom.bins));
    });
  });

  // ---- Restart sweep: a key on each waterfall, shown only while the console says STALLED --------
  // The key is redrawn from each poll's answer (tick), and disabled at once when pressed. The press's
  // answer clears the pending state without a redraw, so the key stays disabled until a poll
  // answers: normally that poll reads "restarting" and hides it.
  var lastPoll = null;        // the last poll's {code, body}, or null after lost contact
  var restartPending = false;
  var pollSeq = 0;            // polls sent so far
  var restartNoteSeq = 0;     // the polls sent when the refusal note went up

  function renderRestart() {
    var key = restartKey(lastPoll, restartPending);
    panels.forEach(function (p) {
      if (!p.restart) return;
      p.restart.hidden = !key.shown;
      p.restart.disabled = key.disabled;
      if (key.disabled) p.restart.setAttribute("aria-busy", "true");
      else p.restart.removeAttribute("aria-busy");
    });
  }

  function postRestart() {
    return fetch("/api/spectrum/restart", { method: "POST", cache: "no-store" })
      .then(function (res) {
        return res.json().then(function (body) {
          body.ok = res.ok;
          body.status = res.status;
          return body;
        });
      }).catch(function (err) {
        return { ok: false, status: 0, reason: "lost contact with the console: " + err.message };
      });
  }

  var restarter = makeRestarter({
    send: postRestart,
    onPending: function (pending) {
      restartPending = pending;
      if (pending) renderRestart();
    },
    onResult: function (res) {
      var note = restartNote(res);
      if (!note) return;
      restartNoteText = note;
      restartNoteSeq = pollSeq;
      renderCaptions();
    },
  });

  panels.forEach(function (p) {
    if (!p.restart) return;
    p.restart.addEventListener("click", function (ev) {
      ev.stopPropagation();
      restarter.press();
    });
  });

  // Keep the badge short and put the full reason in the panel and the badge's title.
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
  }

  // Show what meterState decided: the floor and peak lines for a reading, or the empty meter with
  // the note in place of a floor and a peak the sweep no longer has.
  function showMeter(p, shown) {
    if (shown.note) {
      clearGainMeter(p, shown.note);
    } else if (shown.reading && shown.reading !== p.reading) {
      p.reading = shown.reading;
      updateGainMeter(p);
    }
  }

  function resetView() {
    // Reset the painted history when the span changes or the sweep becomes unavailable. After
    // recovery, the next replay of measured rows can fill the canvas again.
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

  function showUnavailable(body) {
    var down = window.DecoderPanel.downState(body);
    var reason = window.DecoderPanel.refusalText(down);
    if (!waiting) {
      waiting = true;
      span = null;
      live = null;
      liveGain = null;
      radioNote = "Not tuned";
      renderDials();
      renderCaptions();
      renderGain(null, null);
      resetView();
      panels.forEach(function (p) { clearGainMeter(p, "offline"); });
    }
    panels.forEach(function (p) { showReason(p, down); });
    say(down.badge, "na", reason);
    showCommand(null);
  }

  function tick() {
    if (stopped) return;
    var seq = ++pollSeq;
    fetch("/api/spectrum?limit=40", { cache: "no-store" })
      .then(function (res) {
        return res.json().then(function (body) { return { code: res.status, body: body }; });
      })
      .then(function (result) {
        lastPoll = result;
        renderRestart();
        if (restartNoteText && seq > restartNoteSeq) {
          restartNoteText = null;
          renderCaptions();
        }
        if (result.code !== 200 || result.body.available === false) {
          showUnavailable(result.body);
          return;
        }
        showCommand(result.body.command);
        waiting = false;
        panels.forEach(function (p) { showReason(p, null); });
        // The span the sweep is really on, as the server reports it: the dial reads this, and a
        // +/- step counts from it.
        live = { low: result.body.freq_low, high: result.body.freq_high,
                 binSize: result.body.bin_size, center: result.body.center };
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
        panels.forEach(function (p) {
          if (p.meter) showMeter(p, meterState(p.reading, fresh, result.body.stale));
        });

        var badge = sweepBadge(result.body, total);
        say(badge.text, badge.kind, badge.title);
      })
      .catch(function () {
        lastPoll = null;
        renderRestart();
        showUnavailable({ reason: "Lost contact with the console." });
      })
      .then(function () {
        // Poll gently while the radio is away — another program holds the dongle, and hammering
        // the API will not bring it back any sooner.
        if (!stopped) setTimeout(tick, waiting ? RETRY_MS : POLL_MS);
      });
  }

  // A lesson that names its own band (data-tune-low/high on the canvas) points the shared sweep at
  // it once on load, so the waterfall shows that band instead of the default FM sweep. It asks by
  // the band's centre, so the console offsets the sweep to keep that centre on measured bins. The
  // retune is detected on the next poll like any other span change. Polling starts whatever the
  // outcome — on no radio the tune 503s and the panel still shows the honest no-radio state.
  function autoTuneToBand() {
    for (var i = 0; i < panels.length; i++) {
      var c = panels[i].canvas;
      var band = parseTuneBand(c.getAttribute("data-tune-low"), c.getAttribute("data-tune-high"),
                               c.getAttribute("data-tune-bin"));
      // A refusal or no radio needs no handling here: the next poll draws the band honestly.
      if (band) return postTune(bandRequest(band));
    }
    return Promise.resolve();
  }

  say("checking", "sample", "Checking receiver…");
  autoTuneToBand().then(tick);
})();
