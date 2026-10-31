// ABOUTME: Tests the Flipper panel's pure helpers — the identity line and the radio state words.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/flipper.js");

test("the device line is model, firmware and region, joined", () => {
  assert.equal(
    panel.deviceLine({ model: "Flipper Zero", firmware: "1.4.3", region: "US" }),
    "Flipper Zero · fw 1.4.3 · US");
});

test("a missing region is omitted, not shown blank", () => {
  assert.equal(
    panel.deviceLine({ model: "Flipper Zero", firmware: "1.4.3", region: "" }),
    "Flipper Zero · fw 1.4.3");
  assert.equal(panel.deviceLine(null), "");
});

test("the radio state is words, not a colour, and empty when unknown", () => {
  assert.equal(panel.radioLine({ radio_alive: true }), "radio: alive");
  assert.equal(panel.radioLine({ radio_alive: false }), "radio: down");
  assert.equal(panel.radioLine({}), "");
  assert.equal(panel.radioLine(null), "");
});
