// ABOUTME: Tests the low-battery shutdown banner formatter (sigint101/static/power-notice.js).
// ABOUTME: Pure logic only; DOM visibility, 3-theme layout, and z-index are exercised by render-check.

const test = require("node:test");
const assert = require("node:assert");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const p = require("../sigint101/static/power-notice.js");

const POWER_NOTICE_SRC = readFileSync(
  require.resolve("../sigint101/static/power-notice.js"),
  "utf8",
);
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("inactive, null, or malformed /api/power answers stay hidden", () => {
  const hidden = { visible: false, failed: false, headline: "", detail: "" };
  assert.deepEqual(p.powerNoticeView(null), hidden);
  assert.deepEqual(p.powerNoticeView(undefined), hidden);
  assert.deepEqual(p.powerNoticeView({ active: false }), hidden);
  assert.deepEqual(p.powerNoticeView({ active: true }), hidden);
  assert.deepEqual(p.powerNoticeView({ active: true, failed: false }), hidden);
  assert.deepEqual(
    p.powerNoticeView({ active: true, failed: false, remaining_s: NaN }),
    hidden,
  );
  assert.deepEqual(
    p.powerNoticeView({ active: true, failed: true, remaining_s: 30 }),
    hidden,
  );
});

test("active countdown renders remaining seconds and pack voltage without relying on colour", () => {
  assert.deepEqual(
    p.powerNoticeView({
      active: true,
      failed: false,
      remaining_s: 42,
      voltage: 9.21,
      threshold_v: 9.5,
    }),
    {
      visible: true,
      failed: false,
      headline: "LOW BATTERY · POWERING OFF IN 42 s",
      detail: "Pack 9.21 V (limit 9.50 V) · Plug in charger now to cancel",
    },
  );
});

test("zero-second countdown renders POWERING OFF NOW", () => {
  const view = p.powerNoticeView({
    active: true,
    failed: false,
    remaining_s: 0,
    voltage: 9.15,
    threshold_v: 9.5,
  });
  assert.equal(view.visible, true);
  assert.equal(view.failed, false);
  assert.equal(view.headline, "LOW BATTERY · POWERING OFF NOW");
});

test("failed power-off renders explicit failure, retry countdown, and manual shutdown guidance", () => {
  assert.deepEqual(
    p.powerNoticeView({
      active: true,
      failed: true,
      retry_in_s: 47,
      voltage: 9.18,
      threshold_v: 9.5,
    }),
    {
      visible: true,
      failed: true,
      headline: "LOW BATTERY · AUTOMATIC POWER-OFF FAILED · RETRYING IN 47 s · SAVE AND SHUT DOWN NOW",
      detail: "Pack 9.18 V (limit 9.50 V) · Plug in charger now to cancel",
    },
  );
});

test("zero-second retry renders RETRYING NOW", () => {
  const view = p.powerNoticeView({
    active: true,
    failed: true,
    retry_in_s: 0,
    voltage: 9.12,
    threshold_v: 9.5,
  });
  assert.equal(view.visible, true);
  assert.equal(view.failed, true);
  assert.equal(
    view.headline,
    "LOW BATTERY · AUTOMATIC POWER-OFF FAILED · RETRYING NOW · SAVE AND SHUT DOWN NOW",
  );
});

test("F2: superseded late reply cannot resurrect a cleared warning", () => {
  const ctrl = p.createPowerNoticeController({ lifetimeMs: 15000 });
  const seq1 = ctrl.startPoll();
  const seq2 = ctrl.startPoll();

  // Newer poll delivers inactive -> clears banner.
  const v2 = ctrl.applyReply(seq2, { active: false }, 1000);
  assert.equal(v2.visible, false);

  // Older held poll arrives late with a failed countdown -> MUST be ignored!
  const v1 = ctrl.applyReply(
    seq1,
    { active: true, failed: true, retry_in_s: 42, voltage: 9.18, threshold_v: 9.5 },
    1500,
  );
  assert.equal(v1, null);
  assert.equal(ctrl.renderAt(1500).visible, false);
});

test("F2: monotonic countdown advances during unanswered polls and drops past lifetime", () => {
  const ctrl = p.createPowerNoticeController({ lifetimeMs: 15000 });
  const seq1 = ctrl.startPoll();
  const initial = ctrl.applyReply(
    seq1,
    { active: true, failed: true, retry_in_s: 15, voltage: 9.18, threshold_v: 9.5 },
    1000,
  );
  assert.equal(initial.visible, true);
  assert.equal(initial.failed, true);
  assert.match(initial.headline, /RETRYING IN 15 s/);

  // 5 s later with failed/unanswered polls: countdown advances monotonically to 10 s.
  const seq2 = ctrl.startPoll();
  const at5 = ctrl.applyError(seq2, 6000);
  assert.equal(at5.visible, true);
  assert.match(at5.headline, /RETRYING IN 10 s/);

  // 15 s later (at lifetime boundary): countdown reaches 0 s.
  const at15 = ctrl.renderAt(16000);
  assert.equal(at15.visible, true);
  assert.match(at15.headline, /RETRYING NOW/);

  // 24 s later (past 15 s lifetime with no fresh reply): keep warning, drop countdown, say status
  // cannot be read.
  const at24 = ctrl.renderAt(25000);
  assert.equal(at24.visible, true);
  assert.equal(at24.failed, true);
  assert.doesNotMatch(at24.headline, /RETRYING IN \d+ s|RETRYING NOW/);
  assert.match(at24.headline + " " + at24.detail, /status.*unreadable|cannot read/i);

  // Non-failed countdown behaves the same when replies stop arriving.
  const ctrl2 = p.createPowerNoticeController({ lifetimeMs: 15000 });
  const s1 = ctrl2.startPoll();
  ctrl2.applyReply(
    s1,
    { active: true, failed: false, remaining_s: 30, voltage: 9.21, threshold_v: 9.5 },
    1000,
  );
  assert.match(ctrl2.renderAt(7000).headline, /POWERING OFF IN 24 s/);
  const expired = ctrl2.renderAt(20000);
  assert.equal(expired.visible, true);
  assert.equal(expired.failed, false);
  assert.doesNotMatch(expired.headline, /POWERING OFF IN \d+ s|POWERING OFF NOW/);
  assert.match(expired.headline + " " + expired.detail, /status.*unreadable|cannot read/i);

  // Fresh inactive reply clears the expired warning cleanly.
  const s2 = ctrl2.startPoll();
  const cleared = ctrl2.applyReply(s2, { active: false }, 21000);
  assert.equal(cleared.visible, false);
});

test("F2 (r4): seeded banner survives first-poll timeout as unreadable and ignores malformed replies", async () => {
  // Run the real installed DOM script against a server-rendered active warning.
  function runDomFixture(initialState, { fetchImpl }) {
    let now = 0;
    let seq = 0;
    const timeouts = [];
    const intervals = [];
    const classes = new Set(initialState && initialState.failed ? ["power-notice--failed"] : []);
    const headline = { textContent: "LOW BATTERY · POWERING OFF IN 42 s" };
    const detail = { textContent: "Pack 9.21 V (limit 9.50 V) · Plug in charger now to cancel" };
    const banner = {
      hidden: !(initialState && initialState.active === true),
      classList: {
        contains: (c) => classes.has(c),
        toggle(c, force) {
          const next = force === undefined ? !classes.has(c) : Boolean(force);
          if (next) classes.add(c);
          else classes.delete(c);
          return next;
        },
      },
      getAttribute(name) {
        if (name === "data-power-initial") return JSON.stringify(initialState);
        return null;
      },
      querySelector(sel) {
        if (sel === "[data-power-headline]") return headline;
        if (sel === "[data-power-detail]") return detail;
        return null;
      },
    };
    const listeners = {};
    const context = {
      document: {
        querySelector: (sel) => (sel === "[data-power-notice]" ? banner : null),
        addEventListener: (type, fn) => { listeners[type] = fn; },
      },
      performance: { now: () => now },
      AbortController: class {
        constructor() { this.signal = { aborted: false }; }
        abort() { this.signal.aborted = true; }
      },
      fetch: fetchImpl,
      setTimeout(fn, ms) {
        const id = ++seq;
        timeouts.push({ id, at: now + ms, fn });
        return id;
      },
      clearTimeout(id) {
        const i = timeouts.findIndex((t) => t.id === id);
        if (i >= 0) timeouts.splice(i, 1);
      },
      setInterval(fn, ms) {
        const id = ++seq;
        intervals.push({ id, next: now + ms, every: ms, fn });
        return id;
      },
      console,
    };
    context.window = context;
    runInNewContext(POWER_NOTICE_SRC, context);

    async function advance(ms) {
      const until = now + ms;
      for (;;) {
        let nextTime = Infinity;
        let pick = null;
        for (const t of timeouts) {
          if (t.at < nextTime) { nextTime = t.at; pick = { kind: "timeout", item: t }; }
        }
        for (const iv of intervals) {
          if (iv.next < nextTime) { nextTime = iv.next; pick = { kind: "interval", item: iv }; }
        }
        if (!pick || nextTime > until) break;
        now = nextTime;
        if (pick.kind === "timeout") {
          const idx = timeouts.indexOf(pick.item);
          if (idx >= 0) timeouts.splice(idx, 1);
          pick.item.fn();
        } else {
          pick.item.next += pick.item.every;
          pick.item.fn();
        }
        await settle();
        await settle();
      }
      now = until;
      await settle();
    }

    return { banner, headline, detail, advance, triggerPoll: () => listeners["sigint101:power-poll"]() };
  }

  // Case 1: first poll hangs and times out at 5.5 s -> warning stays visible as UNREADABLE.
  let nextReply = "hang";
  const dom = runDomFixture(
    { active: true, failed: false, remaining_s: 42, voltage: 9.21, threshold_v: 9.5 },
    {
      fetchImpl: () => {
        if (nextReply === "hang") return new Promise(() => {});
        const body = nextReply;
        return Promise.resolve({ ok: true, status: 200, json: async () => body });
      },
    },
  );
  await dom.advance(6000);
  assert.equal(dom.banner.hidden, false, "first-poll timeout must not hide seeded warning");
  assert.match(dom.headline.textContent, /LOW BATTERY.*UNREADABLE/i);
  assert.match(dom.detail.textContent, /cannot read/i);

  // Malformed parseable replies MUST NOT clear the warning.
  for (const bad of [{}, null, { active: true }, { active: "false" }, { active: true, failed: false }]) {
    nextReply = bad;
    dom.triggerPoll();
    await settle();
    await settle();
    assert.equal(dom.banner.hidden, false, `malformed reply ${JSON.stringify(bad)} cleared banner`);
  }

  // Explicit valid inactive reply clears the warning.
  nextReply = { active: false };
  dom.triggerPoll();
  await settle();
  await settle();
  assert.equal(dom.banner.hidden, true);
  assert.equal(dom.headline.textContent, "");

  // Case 2: already-polled active warning ignores malformed replies and clears only on {active: false}.
  const ctrl = p.createPowerNoticeController({ lifetimeMs: 15000 });
  const s1 = ctrl.startPoll();
  const v1 = ctrl.applyReply(
    s1,
    { active: true, failed: false, remaining_s: 40, voltage: 9.21, threshold_v: 9.5 },
    1000,
  );
  assert.equal(v1.visible, true);

  for (const bad of [{}, null, { active: true }, { active: "false" }, { active: true, failed: true, remaining_s: 10 }]) {
    const sBad = ctrl.startPoll();
    const vBad = ctrl.applyReply(sBad, bad, 3000);
    assert.equal(vBad.visible, true, `malformed reply ${JSON.stringify(bad)} must act as read failure`);
    assert.match(vBad.headline, /POWERING OFF IN 38 s/);
  }
  const sClear = ctrl.startPoll();
  assert.equal(ctrl.applyReply(sClear, { active: false }, 4000).visible, false);
});

test("F2 (r5): contradictory inactive reply and stale reply after error cannot hide warning", async () => {
  const vm = require("node:vm");
  const fs = require("node:fs");
  const path = require("node:path");
  const src = fs.readFileSync(
    path.join(__dirname, "..", "sigint101", "static", "power-notice.js"),
    "utf8",
  );
  const settle = () => new Promise((r) => setImmediate(r));

  function makeDom(initialObj, fetchImpl) {
    const banner = {
      hidden: !(initialObj && initialObj.active),
      _classes: new Set(initialObj && initialObj.failed ? ["power-notice--failed"] : []),
      classList: {
        toggle(name, on) { if (on) banner._classes.add(name); else banner._classes.delete(name); },
      },
      getAttribute(attr) {
        return attr === "data-power-initial" ? JSON.stringify(initialObj) : null;
      },
      querySelector(sel) {
        if (sel === "[data-power-headline]") return headline;
        if (sel === "[data-power-detail]") return detail;
        return null;
      },
    };
    const headline = { textContent: initialObj && initialObj.active ? "INITIAL" : "" };
    const detail = { textContent: initialObj && initialObj.active ? "INITIAL" : "" };
    const listeners = {};
    let now = 1000;
    const sandbox = {
      document: {
        querySelector: (s) => (s === "[data-power-notice]" ? banner : null),
        addEventListener: (ev, fn) => { listeners[ev] = fn; },
      },
      performance: { now: () => now },
      Date: { now: () => now },
      setTimeout: () => 1,
      clearTimeout: () => {},
      setInterval: () => 1,
      clearInterval: () => {},
      AbortController: class { constructor() { this.signal = {}; } abort() {} },
      fetch: fetchImpl,
    };
    vm.runInNewContext(src, sandbox, { filename: "power-notice.js" });
    return {
      banner,
      headline,
      detail,
      advanceClock: (ms) => { now += ms; },
      triggerPoll: () => listeners["sigint101:power-poll"](),
    };
  }

  // Probe 1: contradictory inactive payload { active: false, failed: true, retry_in_s: 30 }
  // must be classified malformed and keep a seeded warning visible and marked unreadable.
  for (const bogus of [
    { active: false, failed: true, retry_in_s: 30 },
    { active: false, remaining_s: 12 },
    { active: false, extra: 1 },
  ]) {
    assert.equal(p.classifyPowerReply(bogus).kind, "malformed", JSON.stringify(bogus));
  }
  let replyBody = { active: false, failed: true, retry_in_s: 30 };
  const dom1 = makeDom(
    { active: true, failed: false, remaining_s: 42, voltage: 9.21, threshold_v: 9.5 },
    () => Promise.resolve({ ok: true, status: 200, json: async () => replyBody }),
  );
  dom1.triggerPoll();
  await settle();
  await settle();
  assert.equal(dom1.banner.hidden, false, "contradictory inactive payload hid warning");
  assert.match(dom1.headline.textContent, /UNREADABLE/i);

  // Probe 2: poll A (seq 1) pending while poll B (seq 2) returns malformed JSON -> warning
  // marked unreadable; when poll A then resolves with valid { active: false }, it is stale
  // and must NOT hide the warning. Only a newer poll C (seq 3) with { active: false } clears it.
  const resolvers = [];
  const dom2 = makeDom(
    { active: true, failed: false, remaining_s: 42, voltage: 9.21, threshold_v: 9.5 },
    () => new Promise((resolve) => { resolvers.push(resolve); }),
  );
  dom2.triggerPoll(); // poll A (seq 1)
  dom2.triggerPoll(); // poll B (seq 2)
  assert.equal(resolvers.length, 2);

  // Poll B settles first with malformed JSON.
  resolvers[1]({ ok: true, status: 200, json: async () => ({ active: true }) });
  await settle();
  await settle();
  assert.equal(dom2.banner.hidden, false);
  assert.match(dom2.headline.textContent, /UNREADABLE/i);

  // Stale poll A then resolves with valid inactive -> MUST be ignored!
  resolvers[0]({ ok: true, status: 200, json: async () => ({ active: false }) });
  await settle();
  await settle();
  assert.equal(dom2.banner.hidden, false, "stale inactive reply after newer error hid warning");
  assert.match(dom2.headline.textContent, /UNREADABLE/i);

  // Fresh poll C with explicit valid { active: false } clears the warning.
  dom2.triggerPoll(); // poll C (seq 3)
  resolvers[2]({ ok: true, status: 200, json: async () => ({ active: false }) });
  await settle();
  await settle();
  assert.equal(dom2.banner.hidden, true);
  assert.equal(dom2.headline.textContent, "");

  // Probe 3: controller-level check for read error advancing appliedSeq on an active live notice.
  const ctrl = p.createPowerNoticeController({ lifetimeMs: 15000 });
  const s1 = ctrl.startPoll();
  ctrl.applyReply(s1, { active: true, failed: false, remaining_s: 40, voltage: 9.21, threshold_v: 9.5 }, 1000);
  const sA = ctrl.startPoll();
  const sB = ctrl.startPoll();
  assert.notEqual(ctrl.applyError(sB, 3000), null);
  assert.equal(ctrl.applyReply(sA, { active: false }, 3500), null, "stale inactive must be ignored after newer applyError");
  assert.equal(ctrl.hasActive(), true);
  const sC = ctrl.startPoll();
  assert.equal(ctrl.applyReply(sC, { active: false }, 4000).visible, false);
  assert.equal(ctrl.hasActive(), false);
});
