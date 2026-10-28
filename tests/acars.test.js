// ABOUTME: Tests the ACARS panel's pure helpers — the aircraft ident, the summary, and the freq.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/acars.js");

test("the ident is the tail and flight, falling back to a dash", () => {
  assert.equal(panel.ident({ tail: "F-GTAE", flight: "AF7728" }), "F-GTAE AF7728");
  assert.equal(panel.ident({ tail: "F-GTAE" }), "F-GTAE");
  assert.equal(panel.ident({ flight: "AF7728" }), "AF7728");
  assert.equal(panel.ident({}), "—");
  assert.equal(panel.ident(null), "—");
});

test("the summary is the text, with a note for a text-less handshake", () => {
  assert.equal(panel.summary({ text: "#DFB00000/V206" }), "#DFB00000/V206");
  assert.equal(panel.summary({ text: null }), "(no text)");
  assert.equal(panel.summary(null), "(no text)");
});

test("the frequency is shown to three places, blank when unknown", () => {
  assert.equal(panel.freq({ freq_mhz: 131.55 }), "131.550");
  assert.equal(panel.freq({ freq_mhz: null }), "");
  assert.equal(panel.freq(null), "");
});
