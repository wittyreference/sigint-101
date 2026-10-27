// ABOUTME: Tests the decode panel's pure formatters — the ones that turn a decode into a read line.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/decode.js");

test("formatMHz keeps three places so the two FSK tones read apart", () => {
  assert.equal(panel.formatMHz(433_882_000), "433.882");
  assert.equal(panel.formatMHz(433_958_000), "433.958");
});

test("a reading line leads with temperature and humidity when present", () => {
  const line = panel.summarizeReadings({ id: 252, channel: 0, temperature_C: 21.3, humidity: 45 });
  assert.equal(line, "21.3 °C · 45% RH");
});

test("with no temp/humidity it falls back to the device's own fields, minus identity", () => {
  const line = panel.summarizeReadings({ id: 7, channel: "A", code: "efefef", learn: 0 });
  // id and channel are identity, not a reading, so they are dropped.
  assert.ok(line.includes("code efefef"));
  assert.ok(line.includes("learn 0"));
  assert.ok(!line.includes("id "));
  assert.ok(!line.includes("channel"));
});

test("FSK shows both tones, ASK shows one", () => {
  assert.equal(
    panel.toneLabel({ mod: "FSK", freq_hz: 433_882_000, freq2_hz: 433_958_000 }),
    "433.882 / 433.958 MHz");
  assert.equal(
    panel.toneLabel({ mod: "ASK", freq_hz: 433_950_000, freq2_hz: null }),
    "433.950 MHz");
});
