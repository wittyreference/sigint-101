// ABOUTME: Tests the AIS panel's pure helpers — the vessel ident, its position, and its motion.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/ais.js");

test("the ident is the name, falling back to the MMSI", () => {
  assert.equal(panel.ident({ name: "EVER DIADEM", mmsi: 351759000 }), "EVER DIADEM");
  assert.equal(panel.ident({ mmsi: 477553000 }), "MMSI 477553000");
  assert.equal(panel.ident(null), "MMSI undefined");
});

test("the position is lat, lon to four places, with a note until one arrives", () => {
  assert.equal(panel.position({ lat: 47.58283, lon: -122.34583 }), "47.5828, -122.3458");
  assert.equal(panel.position({ lat: null, lon: null }), "no position yet");
  assert.equal(panel.position({}), "no position yet");
  assert.equal(panel.position(null), "no position yet");
});

test("the motion shows speed and course, each only when reported", () => {
  assert.equal(panel.motion({ sog_kt: 12.3, cog_deg: 51 }), "12.3 kt · 51°");
  assert.equal(panel.motion({ sog_kt: 0 }), "0.0 kt");
  assert.equal(panel.motion({ cog_deg: 118 }), "118°");
  assert.equal(panel.motion({}), "");
  assert.equal(panel.motion(null), "");
});
