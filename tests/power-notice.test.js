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
