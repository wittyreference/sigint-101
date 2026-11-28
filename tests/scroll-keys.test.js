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
      querySelectorAll: () => [],
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

test("scroll cues follow real overflow, scroll position and a stage becoming visible", () => {
  const attrs = new Map();
  const clips = {};
  const lines = [{ top: 68, bottom: 86 }, { top: 94, bottom: 112 }];
  const copy = { style: clips, getBoundingClientRect: () => ({ bottom: 190 }) };
  const handlers = {};
  const band = {
    clientHeight: 100, clientTop: 0, scrollHeight: 190, scrollTop: 0,
    getBoundingClientRect: () => ({ top: 0 }),
    querySelector: () => copy,
    addEventListener: (event, handler) => { handlers[event] = handler; },
  };
  const frame = {
    firstElementChild: band,
    toggleAttribute: (name, on) => { attrs.set(name, on); },
  };
  let resize;
  runInNewContext(readFileSync(require.resolve("../sigint101/static/scroll-keys.js"), "utf8"), {
    document: {
      querySelectorAll: () => [frame],
      addEventListener() {},
      createTreeWalker: () => {
        let remaining = 1;
        return { nextNode: () => remaining-- > 0 ? {} : null };
      },
      createRange: () => ({
        selectNodeContents() {},
        getClientRects: () => lines.map(({ top, bottom }) => ({
          top: top - band.scrollTop, bottom: bottom - band.scrollTop,
        })),
      }),
    },
    NodeFilter: { SHOW_TEXT: 4 },
    ResizeObserver: class {
      constructor(fn) { resize = fn; }
      observe() {}
    },
  });

  assert.equal(attrs.get("data-more-below"), true);
  assert.equal(clips.clipPath, "inset(0 0 96px 0)");
  band.scrollTop = 90;
  handlers.scroll();
  assert.equal(attrs.get("data-more-below"), false);
  assert.equal(clips.clipPath, "");
  band.clientHeight = 0;
  resize();
  assert.equal(attrs.get("data-scrollable"), false);
  band.clientHeight = 100;
  band.scrollTop = 0;
  lines[1] = { top: 79, bottom: 97 };
  resize();
  assert.equal(attrs.get("data-more-below"), true);
  assert.equal(clips.clipPath, "", "a whole line at the edge stays visible");
  band.scrollHeight = 101;
  lines[1] = { top: 83, bottom: 101 };
  resize();
  assert.equal(attrs.get("data-more-below"), true);
  assert.equal(clips.clipPath, "inset(0 0 107px 0)", "even one clipped pixel hides the line");
  band.scrollHeight = 100;
  lines[1].bottom = 98;
  resize();
  assert.equal(attrs.get("data-scrollable"), false);
  assert.equal(attrs.get("data-more-below"), false);
});

test("opening a tile grid centres the active tile within view before refreshing cues", () => {
  const events = [];
  const active = { scrollIntoView: options => events.push(["scroll", options.block, options.inline]) };
  const tiles = {
    clientHeight: 400, clientTop: 0, scrollHeight: 600, scrollTop: 0,
    querySelector: sel => sel === ".tile--active" ? active : null,
    addEventListener() {},
  };
  const frame = {
    firstElementChild: tiles,
    toggleAttribute: (name, on) => { events.push(["attr", name, on]); },
  };
  runInNewContext(readFileSync(require.resolve("../sigint101/static/scroll-keys.js"), "utf8"), {
    document: {
      querySelector: sel => sel === ".tiles" ? tiles : null,
      querySelectorAll: sel => sel === ".scroll-frame" ? [frame] : [],
      addEventListener() {},
    },
  });
  assert.deepEqual(events[0], ["scroll", "center", "nearest"]);
  assert.ok(events.some(e => e[0] === "attr"), "scroll-frame cues still refreshed");
});

test("keys scroll the /law reference list when it overflows", () => {
  let onKey;
  let prevented = 0;
  const moves = [];
  const entries = {
    scrollHeight: 980,
    clientHeight: 320,
    scrollBy: (x, y) => moves.push(["by", x, y]),
    scrollTo: (x, y) => moves.push(["to", x, y]),
  };
  runInNewContext(readFileSync(require.resolve("../sigint101/static/scroll-keys.js"), "utf8"), {
    document: {
      querySelectorAll: () => [],
      querySelector: s => s === ".law__entries" ? entries : null,
      addEventListener: (event, handler) => { if (event === "keydown") onKey = handler; },
    },
  });

  onKey({ key: "PageDown", preventDefault: () => { prevented++; } });
  onKey({ key: "End", preventDefault: () => { prevented++; } });
  assert.deepEqual(moves, [["by", 0, 272], ["to", 0, 980]]);
  assert.equal(prevented, 2);
});

test("keys scroll the /field command list when it overflows", () => {
  let onKey;
  let prevented = 0;
  const moves = [];
  const entries = {
    scrollHeight: 1120,
    clientHeight: 360,
    scrollBy: (x, y) => moves.push(["by", x, y]),
    scrollTo: (x, y) => moves.push(["to", x, y]),
  };
  runInNewContext(readFileSync(require.resolve("../sigint101/static/scroll-keys.js"), "utf8"), {
    document: {
      querySelectorAll: () => [],
      querySelector: s => s === ".field__entries" ? entries : null,
      addEventListener: (event, handler) => { if (event === "keydown") onKey = handler; },
    },
  });

  onKey({ key: "ArrowDown", preventDefault: () => { prevented++; } });
  onKey({ key: "PageDown", preventDefault: () => { prevented++; } });
  onKey({ key: "End", preventDefault: () => { prevented++; } });
  assert.deepEqual(moves, [["by", 0, 48], ["by", 0, 312], ["to", 0, 1120]]);
  assert.equal(prevented, 3);

  entries.scrollHeight = 362;   // <= 2 px overflow is ignored
  onKey({ key: "ArrowDown", preventDefault: () => { prevented++; } });
  assert.equal(moves.length, 3);
  assert.equal(prevented, 3);
});
