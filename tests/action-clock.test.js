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

// Codex D-139 F1: storage shared by every tab of one browser, as localStorage is.
function sharedStorage() {
  const stored = {};
  return { getItem: k => (k in stored ? stored[k] : null),
           setItem: (k, v) => { stored[k] = String(v); }, stored };
}

test("one browser keeps one conversion: the tightest any of its tabs has seen", () => {
  // A delay between the console's render and the response reaching the browser only ever makes a
  // tab's estimate of the console's clock lag. Tab B's response was held 950 ms; tab A's was not.
  // B's later action must still stamp after A's earlier one.
  const storage = sharedStorage();
  let now = 0;
  now = 101;
  const a = clock.makeClock({ epoch: "5eed", ms: 1000 }, () => now, 101, storage);
  now = 1452;
  const b = clock.makeClock({ epoch: "5eed", ms: 1500 }, () => now, 1450, storage);
  now = 2000;
  const early = a.stamp().at;
  now = 2100;
  const late = b.stamp().at;
  assert.ok(late > early, `the later action stamped ${late}, before the earlier one's ${early}`);
  assert.equal(late, 2999);
  assert.ok(Object.keys(storage.stored).some(k => k.includes("5eed")), "kept per boot");
});

test("a tab opened before the tightest one picks up its conversion at the next stamp", () => {
  const storage = sharedStorage();
  let now = 1452;
  const b = clock.makeClock({ epoch: "5eed", ms: 1500 }, () => now, 1450, storage);  // held 950
  now = 101;
  clock.makeClock({ epoch: "5eed", ms: 1000 }, () => now, 101, storage);            // prompt
  now = 2100;
  assert.equal(b.stamp().at, 2999);
});

test("without working storage a tab keeps its own conversion and still stamps", () => {
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  let now = 50;
  for (const storage of [broken, null, undefined]) {
    const c = clock.makeClock({ epoch: "5eed", ms: 1000 }, () => now, 50, storage);
    now = 1050;
    assert.deepEqual(c.stamp(), { epoch: "5eed", at: 2000 });
    now = 50;
  }
});

test("a lesson's opening is stamped at its navigation start, on the same conversion", () => {
  // Codex D-139 F2: the first restore is the learner's act of opening the lesson, so it is
  // stamped when the navigation began, not when the script ran.
  let now = 3000;                                        // the script ran late
  const c = clock.makeClock({ epoch: "5eed", ms: 1000 }, () => now, 106, null, 100);
  assert.deepEqual(c.navigationStamp(), { epoch: "5eed", at: 994 });
  assert.equal(clock.makeClock(null, () => 0, 0, null, 0).navigationStamp(), null);
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
