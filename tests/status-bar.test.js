// ABOUTME: Tests updates to the kiosk's global radio and PPM labels from the hardware API.
// ABOUTME: A lost connection or an unrecognised radio state leaves the radio unknown.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

test("the bar refreshes from the real hardware endpoint and clears an unconfirmed radio state", async () => {
  const ppm = { textContent: "PPM —" };
  const radio = { textContent: "RADIO —" };
  const battery = { before() {} };
  const nodes = { "[data-status-ppm]": ppm, "[data-status-radio]": radio,
                  ".readout--batt": battery };
  const calls = [];
  let nextPoll;
  let answer = { state: "present", ppm: null };
  const source = fs.readFileSync(
    require.resolve("../sigint101/static/status-bar.js"), "utf8");
  vm.runInNewContext(source, {
    document: { querySelector: (selector) => nodes[selector] || null },
    window: {},
    fetch: async (url) => {
      calls.push(url);
      if (!answer) throw new Error("connection lost");
      return { ok: true, json: async () => answer };
    },
    setTimeout: (fn) => { nextPoll = fn; },
  });

  const settled = () => new Promise((resolve) => setImmediate(resolve));
  await settled();
  assert.equal(radio.textContent, "RADIO");
  assert.equal(ppm.textContent, "PPM —");
  answer = { state: "busy", ppm: 0 };
  nextPoll();
  await settled();
  assert.equal(radio.textContent, "RADIO BUSY");
  assert.equal(ppm.textContent, "PPM +0");
  answer = { state: "absent", ppm: -12.5 };
  nextPoll();
  await settled();
  assert.equal(radio.textContent, "NO RADIO");
  assert.equal(ppm.textContent, "PPM -12.5");
  answer = null;
  nextPoll();
  await settled();
  assert.equal(radio.textContent, "RADIO —");
  assert.equal(ppm.textContent, "PPM -12.5");
  assert.deepEqual(calls, Array(4).fill("/api/hardware"));
});
