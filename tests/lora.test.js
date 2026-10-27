// ABOUTME: Tests the LoRa panel's pure formatters — the SNR readout and the below-floor label.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/lora.js");

test("SNR reads signed, one place — a negative value is plainly below zero", () => {
  assert.equal(panel.formatSnr(-11.5), "-11.5 dB");
  assert.equal(panel.formatSnr(6.5), "+6.5 dB");
  assert.equal(panel.formatSnr(null), "— dB");
});

test("the floor label names the lesson: a below-floor packet decoded anyway", () => {
  assert.equal(panel.floorLabel({ below_floor: true }), "below floor");
  assert.equal(panel.floorLabel({ below_floor: false }), "above floor");
});
