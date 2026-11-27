// ABOUTME: Tests the shared keyboard-scroll helper — which scroll a key means at a given band size.
// ABOUTME: Includes a keydown check for the operator picker and its overflow threshold.

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
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

test("operator keys scroll the instrument picker when it overflows", () => {
  let onKey;
  let prevented = 0;
  const moves = [];
  const picker = {
    scrollHeight: 1320,
    clientHeight: 394,
    scrollBy: (x, y) => moves.push(["by", x, y]),
    scrollTo: (x, y) => moves.push(["to", x, y]),
  };
  runInNewContext(readFileSync(require.resolve("../sigint101/static/scroll-keys.js"), "utf8"), {
    document: {
      querySelector: s => s === ".rack__picker" ? picker : null,
      addEventListener: (event, handler) => { if (event === "keydown") onKey = handler; },
    },
  });

  function key(key) {
    onKey({ key, preventDefault: () => { prevented++; } });
  }

  for (const name of ["ArrowDown", "ArrowUp", "PageDown", "PageUp", "Home", "End"]) key(name);
  assert.deepEqual(moves, [
    ["by", 0, 48], ["by", 0, -48], ["by", 0, 346], ["by", 0, -346],
    ["to", 0, 0], ["to", 0, 1320],
  ]);
  assert.equal(prevented, 6);

  picker.scrollHeight = 396;   // The other regions ignore overflow of two pixels or less.
  key("ArrowDown");
  assert.equal(moves.length, 6);
  assert.equal(prevented, 6);
});
