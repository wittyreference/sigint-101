// ABOUTME: Tests the page's action clock (action-clock.js): each learner action is stamped on the
// ABOUTME: console's own clock (its epoch, and its time at render plus the page's elapsed time).

const test = require("node:test");
const assert = require("node:assert/strict");
const clock = require("../sigint101/static/action-clock.js");

test("the clock meta names the console's epoch and its time at render", () => {
  assert.deepEqual(clock.parseAnchor("5eed 1234.5"), { epoch: "5eed", ms: 1234.5 });
  assert.equal(clock.parseAnchor(""), null);
  assert.equal(clock.parseAnchor(null), null);
  assert.equal(clock.parseAnchor("5eed"), null);
  assert.equal(clock.parseAnchor("5eed soon"), null);
});

test("a stamp is the console's time at render plus the time since, on the page's own clock", () => {
  let now = 50;
  const c = clock.makeClock({ epoch: "5eed", ms: 1000 }, () => now);
  assert.deepEqual(c.stamp(), { epoch: "5eed", at: 1000 });
  now = 50 + 2500.25;
  assert.deepEqual(c.stamp(), { epoch: "5eed", at: 3500.25 });
});

test("a later action always stamps later, so the console can order a page's actions", () => {
  let now = 0;
  const c = clock.makeClock({ epoch: "5eed", ms: 10 }, () => now);
  const stamps = [];
  for (const step of [0, 0.001, 1, 300]) {
    now += step;
    stamps.push(c.stamp().at);
  }
  assert.deepEqual([...stamps].sort((a, b) => a - b), stamps);
});

test("a page without the clock meta stamps nothing; the console then uses arrival order", () => {
  assert.equal(clock.makeClock(null, () => 0).stamp(), null);
});

test("a stamped body keeps its fields and adds the stamp; no clock leaves it as it was", () => {
  const c = { stamp: () => ({ epoch: "5eed", at: 42 }) };
  assert.deepEqual(clock.stamped({ freq_hz: 144_390_000 }, c),
                   { freq_hz: 144_390_000, epoch: "5eed", at: 42 });
  assert.deepEqual(clock.stamped({ low: 1, high: 2 }, null), { low: 1, high: 2 });
  assert.deepEqual(clock.stamped({ low: 1 }, { stamp: () => null }), { low: 1 });
});
