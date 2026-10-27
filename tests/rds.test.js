// ABOUTME: Tests the RDS panel's pure helpers — whether a station has data, and its name-or-dash.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/rds.js");

test("a station counts as having data once any identity or text arrives", () => {
  assert.equal(panel.hasData(null), false);
  assert.equal(panel.hasData({ group_count: 3 }), false);          // groups but no readable fields
  assert.equal(panel.hasData({ pi: "0x6201" }), true);
  assert.equal(panel.hasData({ ps: "YLEKLAS1" }), true);
  assert.equal(panel.hasData({ radiotext: "Finlandia" }), true);
});

test("the station name falls back to a dash before one is decoded", () => {
  assert.equal(panel.psOrDash({ ps: "YLEKLAS1" }), "YLEKLAS1");
  assert.equal(panel.psOrDash({ pi: "0x6201" }), "—");
  assert.equal(panel.psOrDash(null), "—");
});
