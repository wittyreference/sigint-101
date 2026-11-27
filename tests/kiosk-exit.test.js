// ABOUTME: Exercises Home's two-tap Exit in the kiosk shell.
// ABOUTME: Advances a fake clock and counts form submissions without closing a browser.

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");

test("Exit submits only on a second tap within three seconds", () => {
  const source = readFileSync(require.resolve("../sigint101/templates/base.html"), "utf8");
  const handlers = {};
  const idle = { hidden: false };
  const armed = { hidden: true };
  let now = 1000;
  let timer;
  let submissions = 0;
  const form = {
    addEventListener: () => {},
    requestSubmit: () => submissions++,
    querySelector: selector => ({
      ".btn-exit": { addEventListener: (event, handler) => { handlers[event] = handler; } },
      "[data-exit-idle]": idle, "[data-exit-armed]": armed,
    })[selector],
  };
  for (const [, script] of source.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
    runInNewContext(script, {
      document: { querySelector: selector => selector === ".statusbar__exit" ? form : null },
      performance: { now: () => now },
      setTimeout: (fn, ms) => { timer = fn; assert.equal(ms, 3000); return 1; },
      clearTimeout: () => { timer = undefined; },
    });
  }

  assert.equal(typeof handlers.click, "function", "Exit must arm before it can submit");
  handlers.click();
  assert.equal(submissions, 0);
  assert.equal(idle.hidden, true);
  assert.equal(armed.hidden, false);
  now += 3000;
  timer();
  assert.equal(armed.hidden, true);
  handlers.click();
  assert.equal(submissions, 0);
  now += 100;
  handlers.click();
  assert.equal(submissions, 1);
});
