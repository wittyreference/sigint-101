// ABOUTME: Tests the LoRa panel's pure formatters — the SNR readout and the below-floor label.
// ABOUTME: Covers formatters, rack permit refresh, and the previous-send label in Node.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/lora.js");

test("SNR reads signed, one place — a negative value is plainly below zero", () => {
  assert.equal(panel.formatSnr(-11.5), "-11.5 dB");
  assert.equal(panel.formatSnr(6.5), "+6.5 dB");
  assert.equal(panel.formatSnr(null), "— dB");
});

test("the floor label names the lesson: a below-floor packet decoded anyway", () => {
  assert.equal(panel.floorLabel({ below_floor: true }), "below floor");
  assert.equal(panel.floorLabel({ below_floor: false }), "above floor");
});

test("send outcomes display only what came back from the radio", () => {
  assert.equal(panel.sendOutcome({ status: "handed" }), "handed to the radio");
  assert.equal(panel.sendOutcome({ status: "repeated" }), "a node repeated it");
  assert.equal(panel.sendOutcome({ status: "error", reason: "NO_CHANNEL" }), "NO_CHANNEL");
  assert.equal(panel.sendOutcome({ status: "no-repeat" }),
    "No node repeated it. A mesh of one hears nothing.");
  assert.equal(panel.sendOutcome({ status: "lost", reason: "connection closed" }),
    "connection closed");
});

test("unreported radio values stay unreported and default power is named", () => {
  assert.equal(panel.radioLine({}), "Radio settings not reported");
  assert.equal(panel.radioLine({ settings_read: true, region: "US", preset: null, tx_power: 0 }),
    "US · custom · frequency not reported · radio default");
});

async function rack({ sends = [], postStatus = 403 } = {}) {
  const fs = require("node:fs");
  const vm = require("node:vm");
  const radio = {}, outcome = { dataset: {} }, note = {};
  let click, reloads = 0, posts = 0;
  const strip = {
    dataset: { loraSendPermit: "expired" },
    querySelector(selector) {
      return {
        "[data-lora-send]": { addEventListener: (_name, fn) => { click = fn; } },
        "[data-lora-radio]": radio, "[data-lora-outcome]": outcome,
        "[data-lora-no-repeat]": note,
      }[selector];
    },
  };
  const path = require.resolve("../sigint101/static/lora.js");
  vm.runInNewContext(fs.readFileSync(path, "utf8"), {
    document: { querySelectorAll: () => [{ querySelector: () => ({}) }],
                querySelector: () => strip },
    DecoderPanel: { showLive() {}, quietText() {}, showDown() {} },
    window: { location: { reload: () => { reloads++; } } },
    fetch: async (url) => {
      if (url === "/api/lora/send") {
        posts++;
        return { status: postStatus, json: async () => ({ reason: "Use Send in the LoRa rack." }) };
      }
      return { ok: true, json: async () => ({ sends }) };
    },
    setTimeout() {},
  });
  const settled = () => new Promise((resolve) => setImmediate(resolve));
  await settled();
  return {
    outcome,
    async tap() { click(); await settled(); },
    counts: () => [reloads, posts],
  };
}

test("a stale permit reloads the rack once without repeating the send", async () => {
  const view = await rack();
  await view.tap();
  assert.deepEqual(view.counts(), [1, 1]);
});

test("a fresh rack labels the latest outcome as a previous send", async () => {
  const view = await rack({ sends: [{ status: "repeated" }] });
  assert.equal(view.outcome.textContent, "Previous send · a node repeated it");
  assert.deepEqual(view.counts(), [0, 0]);
});
