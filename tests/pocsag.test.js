// ABOUTME: Tests the POCSAG panel's pure helpers — the kind tag, the cap-code, and the page summary.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/pocsag.js");

test("the kind tag is the short label for each page type", () => {
  assert.equal(panel.kindLabel("alpha"), "ALPHA");
  assert.equal(panel.kindLabel("numeric"), "NUM");
  assert.equal(panel.kindLabel("skyper"), "SKYPER");
  assert.equal(panel.kindLabel("tone"), "TONE");
});

test("the address is the cap code, or a dash when none decoded", () => {
  assert.equal(panel.addr({ address: 1234567 }), "1234567");
  assert.equal(panel.addr({ address: null }), "—");
  assert.equal(panel.addr(null), "—");
});

test("a page summary is its message, with a note for a tone-only page", () => {
  assert.equal(panel.summary({ kind: "alpha", text: "CALL EXT 2210" }), "CALL EXT 2210");
  assert.equal(panel.summary({ kind: "numeric", text: "1234567890" }), "1234567890");
  assert.equal(panel.summary({ kind: "tone", text: null }), "(tone only)");
  assert.equal(panel.summary(null), "");
});
