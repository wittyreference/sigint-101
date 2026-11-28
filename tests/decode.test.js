// ABOUTME: Tests rtl_433 row formatting and the live/replay panel's actual browser script.
// ABOUTME: Controls fetch replies so mode changes can be tested with requests still in flight.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/decode.js");

test("formatMHz keeps three places so the two FSK tones read apart", () => {
  assert.equal(panel.formatMHz(433_882_000), "433.882");
  assert.equal(panel.formatMHz(433_958_000), "433.958");
});

test("a reading line leads with temperature and humidity when present", () => {
  const line = panel.summarizeReadings({ id: 252, channel: 0, temperature_C: 21.3, humidity: 45 });
  assert.equal(line, "21.3 °C · 45% RH");
});

test("with no temp/humidity it falls back to the device's own fields, minus identity", () => {
  const line = panel.summarizeReadings({ id: 7, channel: "A", code: "efefef", learn: 0 });
  // id and channel are identity, not a reading, so they are dropped.
  assert.ok(line.includes("code efefef"));
  assert.ok(line.includes("learn 0"));
  assert.ok(!line.includes("id "));
  assert.ok(!line.includes("channel"));
});

test("FSK shows both tones, ASK shows one", () => {
  assert.equal(
    panel.toneLabel({ mod: "FSK", freq_hz: 433_882_000, freq2_hz: 433_958_000 }),
    "433.882 / 433.958 MHz");
  assert.equal(
    panel.toneLabel({ mod: "ASK", freq_hz: 433_950_000, freq2_hz: null }),
    "433.950 MHz");
});

const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const turn = () => new Promise(resolve => setImmediate(resolve));
const LIVE = { available: true, decodes: [] };
const LIST = { recordings: [{ stem: "capture", instrument: "ism" }] };
const REPLAYING = { mode: "replay", replay: true, state: "replaying", recording: "capture",
  frequency_hz: 433920000, decodes: [] };

function page() {
  const requests = [];
  const timers = [];
  const classes = new Set();
  const node = () => ({
    children: [], hidden: false, textContent: "",
    appendChild(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
    addEventListener(type, fn) { this[type] = fn; },
    setAttribute() {},
  });
  const list = node(), empty = node(), badge = node(), key = node();
  const dial = node(), freq = node(), caption = node();
  dial.querySelector = selector => ({
    "[data-replay-freq]": freq, "[data-replay-caption]": caption,
  })[selector];
  const strip = { classList: { toggle(name, on) { on ? classes.add(name) : classes.delete(name); } },
    querySelector: selector => selector === "[data-replay-dial]" ? dial : null };
  key.closest = () => strip;
  const mark = { content: { cloneNode: () => ({ textContent: "REPLAY" }) } };
  const visible = { querySelector: selector => ({
    "[data-decode-list]": list, "[data-decode-empty]": empty, "[data-decode-badge]": badge,
    "[data-replay-mark]": mark,
  })[selector] };
  const codec = require("../sigint101/static/decoder-panel.js");
  runInNewContext(readFileSync(require.resolve("../sigint101/static/decode.js"), "utf8"), {
    document: {
      querySelectorAll: selector => ({
        "[data-decode-panel]": [visible], "[data-replay-toggle]": [key],
      })[selector] || [],
      createElement: () => node(),
    },
    DecoderPanel: {
      ...codec,
      showLive(el, body) { el.textContent = codec.liveBadge(body).text; },
      showDown(_panel, el, message, body) {
        const state = codec.downState(body);
        el.textContent = state.badge;
        message.textContent = state.reason;
      },
      showCommand() {},
    },
    fetch(url, init = {}) {
      return new Promise(resolve => requests.push({ url, method: init.method || "GET", resolve }));
    },
    setTimeout(fn) { timers.push(fn); },
    clearTimeout() {},
  });
  function take(route, method = "GET") {
    const i = requests.findIndex(q => q.url.split("?")[0] === route && q.method === method);
    assert.ok(i >= 0, "missing " + method + " " + route);
    return requests.splice(i, 1)[0];
  }
  async function answer(route, body, method = "GET", ok = true) {
    take(route, method).resolve({ ok, json: async () => body });
    await turn();
  }
  async function ready() {
    await answer("/api/replay/recordings", LIST);
    await answer("/api/replay/ism", { ...REPLAYING, state: "idle" });
  }
  async function start() {
    key.click();
    await answer("/api/replay/recordings", LIST);
    await answer("/api/replay/ism/start", REPLAYING, "POST");
  }
  return { ready, start, answer, take, key, badge, list, classes, timers, requests };
}

test("a live answer released after Replay cannot paint", async () => {
  const p = page();
  await p.ready();
  const old = p.take("/api/decode");
  await p.start();
  old.resolve({ ok: true, json: async () => LIVE });
  await turn();
  assert.equal(p.badge.textContent, "REPLAY");
  assert.ok(p.classes.has("tune--replay"));
});

test("a replay answer released after Back to live cannot paint", async () => {
  const p = page();
  await p.ready();
  p.take("/api/decode");   // leave the initial live request unanswered
  await p.start();
  p.timers.shift()();
  const old = p.take("/api/replay/ism");
  p.key.click();
  assert.ok(!p.requests.some(q => q.url.startsWith("/api/decode")));
  await p.answer("/api/replay/ism/stop", { ...REPLAYING, state: "stopped" }, "POST");
  await p.answer("/api/decode", LIVE);
  old.resolve({ ok: true, json: async () => REPLAYING });
  await turn();
  assert.equal(p.badge.textContent, "LIVE");
  assert.ok(!p.classes.has("tune--replay"));
});

test("every rendered replay row carries its own REPLAY mark", async () => {
  const p = page();
  await p.ready();
  p.key.click();
  await p.answer("/api/replay/recordings", LIST);
  const row = { model: "Sensor", mod: "ASK", freq_hz: 433920000, readings: {} };
  await p.answer("/api/replay/ism/start", { ...REPLAYING, decodes: [row, row] }, "POST");
  assert.equal(p.list.children.length, 2);
  for (const item of p.list.children) {
    assert.match(item.className, /decoder__item--replay/);
    assert.equal(item.children[0].textContent, "REPLAY");
  }
});

test("a live replaying refusal follows the run started elsewhere", async () => {
  const p = page();
  await p.ready();
  await p.answer("/api/decode", { cause: "replaying", available: false }, "GET", false);
  await p.answer("/api/replay/ism", REPLAYING);
  assert.equal(p.badge.textContent, "REPLAY");
  assert.equal(p.key.textContent, "Back to live");
});

for (const stateFirst of [false, true]) {
  test("Back to live keeps the recording with " + (stateFirst ? "state first" : "list first"),
    async () => {
      const p = page();
      const list = () => p.answer("/api/replay/recordings", LIST);
      const state = () => p.answer("/api/replay/ism", REPLAYING);
      await (stateFirst ? state() : list());
      await (stateFirst ? list() : state());
      assert.equal(p.key.textContent, "Back to live");
      p.key.click();
      await p.answer("/api/replay/ism/stop", { ...REPLAYING, state: "stopped" }, "POST");
      await p.answer("/api/decode", LIVE);
      assert.equal(p.key.textContent, "Replay");
      assert.equal(p.key.disabled, false);
    });
}
