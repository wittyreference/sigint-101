// ABOUTME: Tests the waterfall panel's pure scalers — the dB→colour map and the unit formatters.
// ABOUTME: Uses node's built-in runner; the canvas drawing is deliberately not exercised here.

// The waterfall's colour is the whole point of the panel: a bin's dB level, normalised to 0..1
// against the smoothed floor/ceiling, becomes a pixel colour. That mapping is pure arithmetic, so
// it is tested directly rather than by reading pixels back off a canvas double — a canvas double
// would mostly test the double. The unit formatters (Hz→MHz, dB) are the other pure decisions the
// panel makes about what a learner is shown.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/waterfall.js");

test("the colour scale runs blue at the floor and red at the ceiling", () => {
  // t=0 is the coldest bin (noise floor): no red, a dark blue. t=1 is the hottest (a carrier):
  // full red. The learner reads height-as-heat, so the ends must not drift.
  assert.equal(panel.colour(0), "rgb(0,0,60)");
  assert.equal(panel.colour(1), "rgb(245,30,0)");
});

test("the colour scale clamps out-of-range levels instead of producing junk", () => {
  // floor/ceiling are eased between sweeps, so a sudden bin can land just outside 0..1; the map
  // must clamp rather than emit an rgb() with negative or >255 channels.
  assert.equal(panel.colour(-5), panel.colour(0));
  assert.equal(panel.colour(5), panel.colour(1));
});

// Between the two ends the map crosses blue→cyan→green→yellow→red; every step must still land
// inside the 0..255 byte range a CSS rgb() accepts, or the browser silently drops the pixel.
test("every colour is a well-formed rgb() triple", () => {
  for (const t of [0, 0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 1]) {
    const c = panel.colour(t);
    const m = c.match(/^rgb\((\d+),(\d+),(\d+)\)$/);
    assert.ok(m, `not an rgb() triple: ${c}`);
    for (const chan of m.slice(1)) {
      const v = Number(chan);
      assert.ok(v >= 0 && v <= 255, `channel out of range in ${c}`);
    }
  }
});

// The two unit formatters. Humans read spans in MHz and levels in dB, so the unit and its
// precision travel with the number — a bare figure in the wrong unit is how a panel misleads.
test("a frequency is shown in MHz to one decimal, because that is how spans are read", () => {
  assert.equal(panel.formatMHz(106500000), "106.5 MHz");
  assert.equal(panel.formatMHz(88000000), "88.0 MHz");
  assert.equal(panel.formatMHz(1e9), "1000.0 MHz");
});

test("a dB figure is shown to one decimal", () => {
  assert.equal(panel.formatDb(3.14159), "3.1 dB");
  assert.equal(panel.formatDb(-46), "-46.0 dB");
  assert.equal(panel.formatDb(0), "0.0 dB");
});

// The gain meter's geometry: floor and peak dB mapped to positions on a fixed 60 dB track pinned a
// few dB above the peak. The gap between them (SNR) is the shaded band's height. This is the whole
// quantitative claim the panel makes, so it is tested rather than read off a laid-out DOM.
const close = (a, b) => assert.ok(Math.abs(a - b) < 0.01, `${a} != ${b}`);

test("the gain meter places floor and peak and sizes the SNR gap between them", () => {
  const g = panel.gainMeterGeometry(-70, -42); // hi = -38, span 60
  close(g.peakTop, 100 * 4 / 60);     // peak sits 4 dB below the top of the window
  close(g.floorTop, 100 * 32 / 60);   // floor is 32 dB below the top
  close(g.gapTop, g.peakTop);         // the gap band starts at the peak line
  close(g.gapHeight, g.floorTop - g.peakTop);
  close(g.gapDb, 28);                 // -42 - (-70)
});

test("turning the gain up (a wider gap) grows the shaded band; a closing gap shrinks it", () => {
  const wide = panel.gainMeterGeometry(-75, -40);
  const narrow = panel.gainMeterGeometry(-55, -40);
  assert.ok(wide.gapHeight > narrow.gapHeight);
  assert.ok(wide.gapDb > narrow.gapDb);
});

test("a floor below the window clamps to the track instead of overflowing it", () => {
  const g = panel.gainMeterGeometry(-200, -42); // floor far below the 60 dB window
  close(g.floorTop, 100);              // clamped to the bottom
  assert.ok(g.gapHeight <= 100);
});

// A lesson can name its own band (data-tune-low/high on the canvas). Before polling, the panel
// turns that attribute pair into a /api/tune request; this is the one pure decision in that path
// — a complete pair becomes integer edges, anything partial or malformed yields null so no tune
// is POSTed (the sweep then stays on its default band rather than being pointed at a bad span).
test("a complete band attribute pair becomes integer tune edges", () => {
  assert.deepEqual(panel.parseTuneBand("432920000", "434920000"),
                   { low: 432920000, high: 434920000 });
});

// A bin rtl_power did not measure (its DC copy) arrives as null. The colour scale must be set by the
// measured bins alone: Math.min reads null as 0 dB, which would pin the ceiling of every row to 0.
test("a row's colour bounds come from its measured bins only", () => {
  assert.deepEqual(panel.rowBounds([-30, -10, null, -31]), { lo: -31, hi: -10 });
});

test("a row with no measured bins has no colour bounds", () => {
  assert.equal(panel.rowBounds([null, null]), null);
  assert.equal(panel.rowBounds([]), null);
});

// Red-night keeps night vision: its waterfall must be red-only, not the blue-to-red rainbow. The
// theme declares its ramp as a CSS token (--wf-ramp, a list of colours from floor to peak); a theme
// without one keeps the conventional map.
test("a theme's ramp token parses into its colour stops", () => {
  assert.deepEqual(panel.parseRamp(" #1a0000 #5c0000  #ff5050 "),
                   [[26, 0, 0], [92, 0, 0], [255, 80, 80]]);
});

test("a missing or malformed ramp token means the theme keeps the conventional map", () => {
  assert.equal(panel.parseRamp(""), null);
  assert.equal(panel.parseRamp(undefined), null);
  assert.equal(panel.parseRamp("#ff0000"), null);           // one stop is not a ramp
  assert.equal(panel.parseRamp("#ff0000 red"), null);
});

test("a ramp runs from its first stop at the floor to its last at the peak", () => {
  const stops = panel.parseRamp("#000000 #800000 #ff4040");
  assert.equal(panel.rampColour(0, stops), "rgb(0,0,0)");
  assert.equal(panel.rampColour(1, stops), "rgb(255,64,64)");
  assert.equal(panel.rampColour(0.25, stops), "rgb(64,0,0)");
  assert.equal(panel.rampColour(-3, stops), "rgb(0,0,0)");   // clamped like the default map
  assert.equal(panel.rampColour(9, stops), "rgb(255,64,64)");
});

test("a bin is painted from the theme's ramp when the theme has one", () => {
  const stops = panel.parseRamp("#000000 #ff0000");
  assert.equal(panel.binColour(-10, -30, 20, stops, "#050000"), "rgb(255,0,0)");
  assert.equal(panel.binColour(null, -30, 20, stops, "#050000"), "#050000");
});

test("an unmeasured bin is painted the empty-band colour, never a level", () => {
  // The panel never borrows a neighbour's value for a bin nobody measured: it shows the gap.
  assert.equal(panel.binColour(null, -30, 20, null, "#040806"), "#040806");
  assert.equal(panel.binColour(-30, -30, 20, null, "#040806"), panel.colour(0));
  assert.equal(panel.binColour(-10, -30, 20, null, "#040806"), panel.colour(1));
});

test("a missing or partial band pair yields null (no tune is sent)", () => {
  assert.equal(panel.parseTuneBand(null, null), null);
  assert.equal(panel.parseTuneBand("432920000", null), null);
  assert.equal(panel.parseTuneBand("", ""), null);
  assert.equal(panel.parseTuneBand("notanumber", "434920000"), null);
});

test("a band that names its bin size asks the sweep for that bin", () => {
  assert.deepEqual(panel.parseTuneBand("1089000000", "1091000000", "31250"),
                   { low: 1089000000, high: 1091000000, bin_size: 31250 });
  // No bin (or a malformed one) leaves the request to the sweep's default bin.
  assert.deepEqual(panel.parseTuneBand("1089000000", "1091000000", null),
                   { low: 1089000000, high: 1091000000 });
  assert.deepEqual(panel.parseTuneBand("1089000000", "1091000000", "wide"),
                   { low: 1089000000, high: 1091000000 });
});

// --- real tuning: a tap maps to the frequency of the bin under the finger ------------------------

test("a tap maps to the true frequency of the bin under it, across the live span", () => {
  // 256 FM bins of 78125 Hz from 88 MHz: the middle of the canvas is bin 128, at 98.0 MHz.
  assert.equal(panel.tapToHz(0.5, 88e6, 108e6, 256), 98e6);
  // The left edge is the first bin; the far right is the last bin, a step short of the high edge.
  assert.equal(panel.tapToHz(0, 88e6, 108e6, 256), 88e6);
  assert.equal(panel.tapToHz(0.9999, 88e6, 108e6, 256), 108e6 - 78125);
  // Anywhere inside a bin's pixel column is that bin.
  assert.equal(panel.tapToHz(10.6 / 256, 88e6, 108e6, 256), 88e6 + 10 * 78125);
});

test("a tap maps the span itself, not the dial +/- a fixed width", () => {
  // The old tap mapped onto dial +/- 1.3 MHz whatever the span. A 2 MHz span has its own edges.
  assert.equal(panel.tapToHz(0.25, 1089e6, 1091e6, 64), 1089.5e6);
});

test("before any row is drawn a tap falls back to the span's own edges", () => {
  assert.equal(panel.tapToHz(0.5, 88e6, 108e6, 0), 98e6);
});

test("a tap off the canvas edge is held to the span", () => {
  assert.equal(panel.tapToHz(-0.2, 88e6, 108e6, 256), 88e6);
  assert.equal(panel.tapToHz(1.3, 88e6, 108e6, 256), 108e6 - 78125);
});

test("a retune keeps the span's width and centres it on the target", () => {
  assert.deepEqual(panel.spanAround(98.3e6, 20e6), { low: 88.3e6, high: 108.3e6 });
  assert.deepEqual(panel.spanAround(91515625, 20e6), { low: 81515625, high: 101515625 });
});

test("the dial reads MHz to the kHz", () => {
  assert.equal(panel.formatTuneMHz(98e6), "98.000");
  assert.equal(panel.formatTuneMHz(91515625), "91.516");
});

// --- the tuner coalesces taps into one /api/tune request at a time ------------------------------

function harness(t, confirmed) {
  // Drives makeTuner with node's fake clock. `send` records each request and hands back a promise
  // the test settles, standing in for the POST at the console boundary.
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const sent = [];
  const results = [];
  const pending = [];
  let resolveSend = null;
  const state = { span: confirmed };
  const tuner = panel.makeTuner({
    stepHz: 100000,
    delayMs: 400,
    confirmed: () => state.span,
    send: (req) => { sent.push(req); return new Promise((res) => { resolveSend = res; }); },
    onPending: (hz) => pending.push(hz),
    onResult: (res) => {
      results.push(res);
      if (res.ok) state.span = { low: res.low, high: res.high, binSize: res.bin_size };
    },
  });
  const settle = async (res) => { resolveSend(res); await new Promise((r) => setImmediate(r)); };
  return { tuner, sent, results, pending, settle, state };
}

const FM = { low: 88e6, high: 108e6, binSize: 125000 };

test("three quick + taps become one retune three steps up", (t) => {
  const h = harness(t, FM);
  h.tuner.step(1); h.tuner.step(1); h.tuner.step(1);
  t.mock.timers.tick(399);
  assert.equal(h.sent.length, 0, "sent before the taps went quiet");
  t.mock.timers.tick(1);
  assert.deepEqual(h.sent, [{ low: 88.3e6, high: 108.3e6, bin_size: 125000 }]);
});

test("the pending target is shown while the taps are being gathered", (t) => {
  const h = harness(t, FM);
  h.tuner.step(1); h.tuner.step(-1); h.tuner.step(-1);
  assert.deepEqual(h.pending, [98.1e6, 98e6, 97.9e6]);
});

test("a tap on the waterfall replaces any pending steps with its own frequency", (t) => {
  const h = harness(t, FM);
  h.tuner.step(1);
  h.tuner.tapTo(91515625);
  t.mock.timers.tick(400);
  assert.deepEqual(h.sent, [{ low: 81515625, high: 101515625, bin_size: 125000 }]);
});

test("taps during a request are sent after it, counted from the span it confirmed", async (t) => {
  const h = harness(t, FM);
  h.tuner.step(1);
  t.mock.timers.tick(400);
  assert.equal(h.sent.length, 1);
  h.tuner.step(1);                           // lands while the first request is in flight
  t.mock.timers.tick(400);
  assert.equal(h.sent.length, 1, "two requests in flight at once");
  await h.settle({ ok: true, low: 88.1e6, high: 108.1e6, bin_size: 125000 });
  t.mock.timers.tick(400);
  assert.deepEqual(h.sent[1], { low: 88.2e6, high: 108.2e6, bin_size: 125000 });
});

test("a refused tune is reported and the confirmed span stays put", async (t) => {
  const h = harness(t, FM);
  h.tuner.step(1);
  t.mock.timers.tick(400);
  await h.settle({ ok: false, status: 400, reason: "outside it" });
  assert.equal(h.results[0].ok, false);
  assert.deepEqual(h.state.span, FM);
  assert.equal(h.pending[h.pending.length - 1], null, "the refused target is still shown as pending");
});

test("with no confirmed span there is nothing to step from, so nothing is sent", (t) => {
  const h = harness(t, null);
  h.tuner.step(1);
  t.mock.timers.tick(400);
  assert.equal(h.sent.length, 0);
});

// --- gain (M2): a stepper over the tuner's real gain settings --------------------------------------

test("the gain ladder runs from automatic up through real R82xx tuner settings", () => {
  // Every rung is an entry in the tuner's own gain table, so rtl_power applies it as asked.
  assert.equal(panel.nextGain(null, 1), 0);                   // "from the bottom up"
  assert.equal(panel.nextGain(0, 1), 7.7);
  assert.equal(panel.nextGain(44.5, 1), 49.6);
  assert.equal(panel.nextGain(49.6, 1), 49.6);                // the top holds
  assert.equal(panel.nextGain(0, -1), null);                  // below the bottom rung is automatic
  assert.equal(panel.nextGain(null, -1), null);
});

test("a gain set off the ladder steps to the nearest rung in that direction", () => {
  assert.equal(panel.nextGain(29.7, 1), 33.8);
  assert.equal(panel.nextGain(29.7, -1), 28.0);
});

test("the gain reads as AUTO or in dB", () => {
  assert.equal(panel.formatGain(null), "AUTO");
  assert.equal(panel.formatGain(0), "0.0 dB");
  assert.equal(panel.formatGain(49.6), "49.6 dB");
});

function gainHarness(t, gain) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const sent = [];
  const pending = [];
  let resolveSend = null;
  const state = gain === undefined ? null : { gain };
  const stepper = panel.makeGainStepper({
    delayMs: 400,
    confirmed: () => state,
    send: (g) => { sent.push(g); return new Promise((res) => { resolveSend = res; }); },
    onPending: (p) => pending.push(p),
    onResult: (res) => { if (res.set) state.gain = res.gain; },
  });
  const settle = async (res) => { resolveSend(res); await new Promise((r) => setImmediate(r)); };
  return { stepper, sent, pending, settle, state };
}

test("quick gain taps become one change, counted up the ladder from the confirmed gain", (t) => {
  const h = gainHarness(t, null);
  h.stepper.step(1); h.stepper.step(1); h.stepper.step(1);
  assert.deepEqual(h.pending.map((p) => p && p.value), [0, 7.7, 14.4]);
  t.mock.timers.tick(400);
  assert.deepEqual(h.sent, [14.4]);
});

test("stepping back down to automatic sends null, a real setting", (t) => {
  const h = gainHarness(t, 0);
  h.stepper.step(-1);
  t.mock.timers.tick(400);
  assert.deepEqual(h.sent, [null]);
});

test("a tap that would not change the gain sends nothing", (t) => {
  const h = gainHarness(t, 49.6);
  h.stepper.step(1);
  t.mock.timers.tick(400);
  assert.deepEqual(h.sent, []);
});

test("with no radio there is no gain to step from, so nothing is sent", (t) => {
  const h = gainHarness(t, undefined);
  h.stepper.step(1);
  t.mock.timers.tick(400);
  assert.deepEqual(h.sent, []);
});

test("the gain meter reads the sweep's median floor and peak, not the row minimum", () => {
  // A real FM row starts with hop-edge roll-off near -15 dB while the floor is about -9.5 dB; the
  // minimum would put FLOOR on the roll-off and overstate the SNR gap.
  const row = { floor_db: -9.5, peak_db: 20.2, dbm: [-15.3, -14.3, -9.5, 20.2, -9.4] };
  assert.deepEqual(panel.meterReading(null, row), { floor: -9.5, peak: 20.2 });
  // Eased from the previous reading so the meter does not flicker between sweeps.
  const next = panel.meterReading({ floor: -10, peak: 20 }, { floor_db: -9, peak_db: 25 });
  assert.ok(Math.abs(next.floor - -9.8) < 1e-9 && Math.abs(next.peak - 21) < 1e-9);
});
