// ABOUTME: Tests the status bar's battery refresh — what a /api/battery answer draws on the gauge.
// ABOUTME: Pure logic only; the 30 s poll and the DOM writes are exercised by HIL render.

const test = require("node:test");
const assert = require("node:assert");
const b = require("../sigint101/static/battery.js");

// A real /api/battery answer from nomad-b on charge (2026-11-03).
const REAL = { present: true, percent: 92, voltage: 12.328, current_a: 0.114,
               charging: true, low: false };

test("a reading draws its percent, its fill, its volts and the bolt", () => {
  assert.deepEqual(b.batteryView(REAL), {
    text: "92%", width: 92, low: false, charging: true, title: "Battery 12.33 V",
  });
});

test("a discharging, low pack drops the bolt and takes the low style", () => {
  const v = b.batteryView({ present: true, percent: 11, voltage: 9.4, current_a: -0.6,
                            charging: false, low: true });
  assert.equal(v.charging, false);
  assert.equal(v.low, true);
  assert.equal(v.text, "11%");
});

test("no reading is a dash and an empty gauge, never a number", () => {
  const blank = { text: "—", width: 0, low: false, charging: false, title: "Battery" };
  assert.deepEqual(b.batteryView({ present: false, reason: "battery daemon unreachable" }), blank);
  assert.deepEqual(b.batteryView(null), blank);      // the console itself did not answer
  assert.deepEqual(b.batteryView({ present: true }), blank);   // a reading with no percent
});
