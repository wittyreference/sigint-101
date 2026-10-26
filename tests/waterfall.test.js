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
