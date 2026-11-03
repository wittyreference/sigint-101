// ABOUTME: Tests the shared keyboard-scroll helper — which scroll a key means at a given band size.
// ABOUTME: Pure logic only; the DOM wiring (keydown, region lookup) is exercised by HIL render.

const test = require("node:test");
const assert = require("node:assert");
const k = require("../sigint101/static/scroll-keys.js");

test("arrows scroll by a fixed step", () => {
  assert.deepEqual(k.scrollAction("ArrowDown", false, 400), { dy: 48 });
  assert.deepEqual(k.scrollAction("ArrowUp", false, 400), { dy: -48 });
});

test("page keys and Space move by about one band-height", () => {
  assert.equal(k.scrollAction("PageDown", false, 400).dy, 352);   // 400 - 48
  assert.equal(k.scrollAction("PageUp", false, 400).dy, -352);
  assert.equal(k.scrollAction(" ", false, 400).dy, 352);
  assert.equal(k.scrollAction(" ", true, 400).dy, -352);          // Shift+Space scrolls up
});

test("Home and End jump to the ends", () => {
  assert.deepEqual(k.scrollAction("Home", false, 400), { to: "top" });
  assert.deepEqual(k.scrollAction("End", false, 400), { to: "bottom" });
});

test("a page never shrinks below 80px on a short band", () => {
  assert.equal(k.scrollAction("PageDown", false, 50).dy, 80);
  assert.equal(k.scrollAction("PageDown", false, 0).dy, 80);
});

test("keys we do not own are ignored", () => {
  assert.equal(k.scrollAction("Enter", false, 400), null);
  assert.equal(k.scrollAction("a", false, 400), null);
  assert.equal(k.scrollAction("Tab", false, 400), null);
});
