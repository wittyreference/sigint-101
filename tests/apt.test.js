// ABOUTME: Tests the APT panel's pure helpers — the image caption (satellite + time) and the src.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/apt.js");

test("the label is the satellite and the received time, trimmed", () => {
  assert.equal(
    panel.label({ satellite: "NOAA-19", received_at: "2026-10-30T12:00:00+00:00" }),
    "NOAA-19 · 2026-10-30 12:00");
  assert.equal(panel.label({ satellite: "NOAA-18" }), "NOAA-18");
  assert.equal(panel.label({}), "NOAA");
  assert.equal(panel.label(null), "NOAA");
});

test("the src points at /media for the decoded png", () => {
  assert.equal(panel.src({ path: "apt/NOAA-19/avhrr.png" }), "/media/apt/NOAA-19/avhrr.png");
  assert.equal(panel.src({}), "/media/");
  assert.equal(panel.src(null), "/media/");
});
