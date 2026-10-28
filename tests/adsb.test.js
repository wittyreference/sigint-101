// ABOUTME: Tests the ADS-B radar's pure helpers — scope scaling and the range/bearing placement.
// ABOUTME: Uses node's built-in runner; the polling and SVG DOM wiring are not exercised here.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/adsb.js");

test("the scope rounds its outer ring up to a nice range", () => {
  assert.equal(panel.scopeRange([]), 50);                       // empty sky: a sensible default
  assert.equal(panel.scopeRange([{ range_nm: 8 }]), 10);        // nearest nice range that holds it
  assert.equal(panel.scopeRange([{ range_nm: 40 }]), 50);
  assert.equal(panel.scopeRange([{ range_nm: 120 }]), 150);
});

test("the scope ignores unlocated contacts when scaling", () => {
  assert.equal(panel.scopeRange([{ range_nm: null }, { range_nm: 8 }]), 10);
});

test("a contact due north sits straight up from the centre", () => {
  const p = panel.plot(50, 0, 50);                              // full range, bearing 0 = north
  assert.ok(Math.abs(p.x - 100) < 0.01);                        // no east/west offset
  assert.ok(Math.abs(p.y - 10) < 0.01);                         // at the top (y = center - radius)
});

test("a contact due east sits to the right of the centre", () => {
  const p = panel.plot(50, 90, 50);                             // full range, bearing 90 = east
  assert.ok(Math.abs(p.x - 190) < 0.01);                        // at the right edge
  assert.ok(Math.abs(p.y - 100) < 0.01);                        // no north/south offset
});

test("range beyond the outer ring is pinned to the edge, never dropped", () => {
  const p = panel.plot(500, 0, 50);                             // far past the scale
  assert.ok(Math.abs(p.y - 10) < 0.01);                         // clamped to the outer ring
});

test("a contact's label is its callsign, falling back to the ICAO hex", () => {
  assert.equal(panel.planeLabel({ callsign: "UAL123", hex_ident: "A1B2C3" }), "UAL123");
  assert.equal(panel.planeLabel({ hex_ident: "A1B2C3" }), "A1B2C3");
});

test("a body has aircraft only when the list is non-empty", () => {
  assert.equal(panel.hasAircraft(null), false);
  assert.equal(panel.hasAircraft({ aircraft: [] }), false);
  assert.equal(panel.hasAircraft({ aircraft: [{ hex_ident: "A1B2C3" }] }), true);
});
