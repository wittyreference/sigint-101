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

test("send outcomes display only what came back from the radio", () => {
  assert.equal(panel.sendOutcome({ status: "handed" }), "handed to the radio");
  assert.equal(panel.sendOutcome({ status: "repeated" }), "a node repeated it");
  assert.equal(panel.sendOutcome({ status: "error", reason: "NO_CHANNEL" }), "NO_CHANNEL");
  assert.equal(panel.sendOutcome({ status: "no-repeat" }),
    "No node repeated it. A mesh of one hears nothing.");
  assert.equal(panel.sendOutcome({ status: "lost", reason: "connection closed" }),
    "connection closed");
});

test("unreported radio values stay unreported and default power is named", () => {
  assert.equal(panel.radioLine({}), "Radio settings not reported");
  assert.equal(panel.radioLine({ settings_read: true, region: "US", preset: null, tx_power: 0 }),
    "US · custom · frequency not reported · radio default");
});
