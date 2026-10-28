// ABOUTME: Tests the APRS panel's pure helpers — the kind tag and the one-line packet summary.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/aprs.js");

test("the kind tag is the short label for each packet type", () => {
  assert.equal(panel.kindLabel("position"), "POS");
  assert.equal(panel.kindLabel("message"), "MSG");
  assert.equal(panel.kindLabel("status"), "STATUS");
  assert.equal(panel.kindLabel("other"), "·");
});

test("a position summary shows decimal lat/lon with its comment", () => {
  assert.equal(
    panel.summary({ kind: "position", lat: 40.7, lon: -74.0, text: "Test de KD2ABC" }),
    "40.700, -74.000 · Test de KD2ABC");
  assert.equal(panel.summary({ kind: "position", lat: 42.425, lon: -71.253 }),
    "42.425, -71.253");
});

test("a message summary names who it was addressed to", () => {
  assert.equal(panel.summary({ kind: "message", addressee: "KD2ABC", text: "Hello there" }),
    "→KD2ABC: Hello there");
});

test("a status or other packet falls back to its text, then its dest", () => {
  assert.equal(panel.summary({ kind: "status", text: "Monitoring 146.52" }), "Monitoring 146.52");
  assert.equal(panel.summary({ kind: "other", dest: "APRS" }), "APRS");
  assert.equal(panel.summary(null), "");
});
