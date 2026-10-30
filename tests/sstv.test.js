// ABOUTME: Tests the SSTV panel's pure helpers — the image caption (mode + time) and the src.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/sstv.js");

test("the label is the SSTV mode, with the received time when present", () => {
  assert.equal(panel.label({ mode: "Martin 1" }), "Martin 1");
  assert.equal(panel.label({}), "unknown mode");
  assert.equal(panel.label(null), "unknown mode");
  // A decode with a timestamp appends a UTC time after the mode.
  const withTime = panel.label({ mode: "Scottie 1", received_at: 1793151825 });
  assert.ok(withTime.startsWith("Scottie 1 · "));
  assert.ok(withTime.endsWith(" UTC"));
});

test("the src points at /media for the decoded png", () => {
  assert.equal(panel.src({ path: "sstv/20261030T120000-000.png" }),
    "/media/sstv/20261030T120000-000.png");
  assert.equal(panel.src({}), "/media/");
  assert.equal(panel.src(null), "/media/");
});
