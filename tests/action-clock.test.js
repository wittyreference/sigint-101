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

test("a stamp is the console's time at render plus the time since the page received it", () => {
  // `received` is when the response's first byte reached the browser, on the browser's shared
  // timeline (performance.timeOrigin + responseStart): when the script ran does not enter.
  let now = 50;
  const c = clock.makeClock({ epoch: "5eed", ms: 1000 }, () => now, 50);
  assert.deepEqual(c.stamp(), { epoch: "5eed", at: 1000 });
  now = 50 + 2500.25;
  assert.deepEqual(c.stamp(), { epoch: "5eed", at: 3500.25 });
});

test("two tabs stamp in the order the learner acted, however long each took to run its script", () => {
  // Codex D-132 F1: the browser's timeline is shared by every tab. Tab A's response arrived at
  // 1,001 and its script ran at once; tab B's arrived at 1,040 (rendered at 1,039) and its script
  // ran 300 ms late. The learner acted in A at 1,260 and in B at 1,300.
  let now = 0;
  const run = (renderMs, received, scriptAt) => {
    now = scriptAt;
    return clock.makeClock({ epoch: "5eed", ms: renderMs }, () => now, received);
  };
  const a = run(1000, 1001, 1001);
  const b = run(1039, 1040, 1340);
  now = 1260;
  const atA = a.stamp().at;
  now = 1300;
  const atB = b.stamp().at;
  assert.ok(atB > atA, `the later action stamped ${atB}, before the earlier one's ${atA}`);
  assert.equal(atA, 1259);
  assert.equal(atB, 1299);
});

test("the browser's own timeline and receive time are what the page reads", () => {
  const perf = {
    timeOrigin: 1_700_000_000_000,
    now: () => 1500,
    getEntriesByType: kind => (kind === "navigation" ? [{ responseStart: 120.5 }] : []),
  };
  assert.equal(clock.browserNow(perf), 1_700_000_001_500);
  assert.equal(clock.receivedAt(perf), 1_700_000_000_120.5);
  assert.equal(clock.receivedAt({ ...perf, getEntriesByType: () => [] }), null);
  assert.equal(clock.receivedAt({ ...perf, getEntriesByType: () => [{ responseStart: 0 }] }), null);
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

test("a document the browser served from its cache stamps nothing, so arrival orders it", () => {
  // Review round 5: Back served lesson A from the HTTP cache with its old clock meta, so its
  // stamps lagged by the time the learner was away. A cached document cannot be mapped, so its
  // requests are ordered by their arrival instead (the console sends pages no-store anyway).
  const perf = (nav) => ({ timeOrigin: 0, now: () => 0, getEntriesByType: () => [nav] });
  assert.equal(clock.fromCache(perf({ type: "back_forward", transferSize: 0, responseStart: 5 })),
               true);
  assert.equal(clock.fromCache(perf({ type: "back_forward", transferSize: 4200, responseStart: 5 })),
               false);
  assert.equal(clock.fromCache(perf({ type: "navigate", transferSize: 4200, responseStart: 5 })),
               false);
  assert.equal(clock.fromCache(perf({ type: "navigate", deliveryType: "cache", transferSize: 0,
                                      responseStart: 5 })), true);
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
