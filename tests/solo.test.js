// ABOUTME: Tests the Tier C solo UI helpers and two-step call state machine (sigint101/static/solo.js).
// ABOUTME: Covers solo marks, leak-free pre-pass line, Step A/Step B transitions, and error handling.

const test = require("node:test");
const assert = require("node:assert/strict");

// Load LiveFind first so solo.js can reuse LiveFind.clock just as the browser does.
global.window = global.window || {};
global.window.LiveFind = require("../sigint101/static/find.js");
const solo = require("../sigint101/static/solo.js");

test("solo mark map covers all six solo states with distinct labels and classes", () => {
  const states = ["idle", "active", "reception_passed", "passed", "call_missed", "expired"];
  const words = new Set();
  const classes = new Set();
  for (const st of states) {
    const m = solo.markFor(st);
    assert.ok(m.word && m.cls);
    words.add(m.word);
    classes.add(m.cls);
  }
  assert.equal(words.size, 6);
  assert.equal(classes.size, 6);
  assert.equal(solo.markFor("reception_passed").word, "CALL");
  assert.equal(solo.markFor("passed").word, "PASSED");
  assert.equal(solo.markFor("call_missed").word, "MISSED");
});

test("huntEvidenceLine never leaks instrument, tool, or frequency before pass", () => {
  assert.equal(
    solo.huntEvidenceLine({ state: "active", passed_before_date: "2026-11-28", error: null }),
    "Passed before · 2026-11-28",
  );
  assert.equal(
    solo.huntEvidenceLine({ state: "expired", reason: "clock_moved", passed_before_date: "2026-11-28" }),
    "The clock changed. Start again.",
  );
  assert.equal(
    solo.huntEvidenceLine({ state: "active", error: "the solo start could not be logged" }),
    "the solo start could not be logged",
  );
  assert.equal(solo.huntEvidenceLine({ state: "idle", passed_before_date: null }), "");
});

test("two-step call state transitions enforce Step A -> Step B -> Change call and Submit gating", () => {
  let ui = solo.initialCallState();
  assert.equal(ui.step, "a");
  assert.equal(ui.call, null);
  assert.equal(ui.whyKey, null);
  assert.equal(solo.canSubmitCall(ui), false);

  // Pick a call on Step A -> moves to Step B, Submit still disabled until a why chip is picked.
  ui = solo.stepTransition(ui, { type: "pick_call", call: "legal" });
  assert.equal(ui.step, "b");
  assert.equal(ui.call, "legal");
  assert.equal(ui.whyKey, null);
  assert.equal(solo.canSubmitCall(ui), false);

  // Pick a why chip -> Submit enabled.
  ui = solo.stepTransition(ui, { type: "pick_why", whyKey: "readily-accessible" });
  assert.equal(ui.step, "b");
  assert.equal(ui.whyKey, "readily-accessible");
  assert.equal(solo.canSubmitCall(ui), true);

  // Change call -> returns to Step A and clears whyKey.
  ui = solo.stepTransition(ui, { type: "change_call" });
  assert.equal(ui.step, "a");
  assert.equal(ui.call, null);
  assert.equal(ui.whyKey, null);
  assert.equal(solo.canSubmitCall(ui), false);
});

test("solo reuses LiveFind.clock for countdown formatting", () => {
  assert.equal(solo.clock(875), "14:35");
  assert.equal(solo.clock(0), "00:00");
  assert.equal(solo.clock(-5), "00:00");
});
