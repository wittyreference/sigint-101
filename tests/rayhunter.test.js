// ABOUTME: Tests the Rayhunter panel's pure helpers — the recording line's text and its state class.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/rayhunter.js");

test("the line text names the recording state", () => {
  assert.equal(panel.lineText("clean"), "recording · clean");
  assert.equal(panel.lineText("warning"), "warning · look closer");
  assert.equal(panel.lineText("off"), "not recording");
  assert.equal(panel.lineText(undefined), "—");
});

test("the line class carries the state by more than colour", () => {
  assert.equal(panel.lineClass("clean"), "rayhunter__line rayhunter__line--clean");
  assert.equal(panel.lineClass("warning"), "rayhunter__line rayhunter__line--warning");
  assert.equal(panel.lineClass("off"), "rayhunter__line");
  assert.equal(panel.lineClass(undefined), "rayhunter__line");
});

test("recording with no analysis report yet reads as recording, never as clean", () => {
  // No report has been read to call clean, so the line says so and stays out of the green.
  assert.equal(panel.lineText("no-report"), "recording · no report yet");
  assert.equal(panel.lineClass("no-report"), "rayhunter__line");
});
