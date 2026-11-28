// ABOUTME: Tests the low-battery shutdown banner formatter (sigint101/static/power-notice.js).
// ABOUTME: Pure logic only; DOM visibility, 3-theme layout, and z-index are exercised by render-check.

const test = require("node:test");
const assert = require("node:assert");
const p = require("../sigint101/static/power-notice.js");

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
