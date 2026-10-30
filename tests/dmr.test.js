// ABOUTME: Tests the DMR/P25 panel's pure helpers — the call ident and the talking parties.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/dmr.js");

test("the ident is the protocol, with the DMR slot when the frame named one", () => {
  assert.equal(panel.ident({ protocol: "DMR", slot: 2 }), "DMR · slot 2");
  assert.equal(panel.ident({ protocol: "P25", slot: null }), "P25");
  assert.equal(panel.ident({ protocol: "DMR" }), "DMR");
});

test("the parties are the talkgroup and source, each only when decoded", () => {
  assert.equal(panel.parties({ talkgroup: 2001, source: 1234567 }), "TG 2001 · SRC 1234567");
  assert.equal(panel.parties({ talkgroup: 21001 }), "TG 21001");
  assert.equal(panel.parties({ source: 21002 }), "SRC 21002");
  assert.equal(panel.parties({}), "");
  assert.equal(panel.parties(null), "");
});
