// ABOUTME: Tests the lesson-stage navigation rules — where a Next/Previous step lands and how each
// ABOUTME: rail pip reads. Pure logic only; the DOM wiring is exercised by the 800x480 render check.

const test = require("node:test");
const assert = require("node:assert");
const nav = require("../sigint101/static/lesson-nav.js");

test("Next and Previous step one panel at a time", () => {
  assert.equal(nav.stepTo(2, 1, 6), 3);
  assert.equal(nav.stepTo(2, -1, 6), 1);
});

test("a step off either end lands nowhere", () => {
  assert.equal(nav.stepTo(0, -1, 6), -1);   // no Previous before the first stage
  assert.equal(nav.stepTo(5, 1, 6), -1);    // Next on the last stage completes instead
  assert.equal(nav.stepTo(0, 1, 0), -1);    // nothing rendered
});

test("the open stage reads active", () => {
  assert.equal(nav.pipState(2, 2, 4, false), "active");
});

test("every stage reached before, behind or ahead of the open one, stays tappable", () => {
  assert.equal(nav.pipState(0, 2, 4, false), "done");   // went back past it
  assert.equal(nav.pipState(4, 2, 4, false), "done");   // reached, then stepped back from
});

test("a stage not reached yet waits", () => {
  assert.equal(nav.pipState(5, 2, 4, false), "ahead");
});

test("a sealed stage stays locked whatever the walk says", () => {
  // Before the commit the sealed stages are not rendered at all; nothing about the walk opens them.
  assert.equal(nav.pipState(4, 3, 5, true), "locked");
  assert.equal(nav.pipState(4, 4, 5, true), "locked");
});
