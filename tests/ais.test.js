// ABOUTME: Tests the AIS panel's pure helpers — the vessel ident, its position, and its motion.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const panel = require("../sigint101/static/ais.js");

const DECODER_PANEL_SRC = readFileSync(
  require.resolve("../sigint101/static/decoder-panel.js"),
  "utf8"
);
const AIS_SRC = readFileSync(require.resolve("../sigint101/static/ais.js"), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

function makeEl({ attrs = {}, dataset = {}, classes = [], text = "" } = {}) {
  const classSet = new Set(classes);
  const listeners = {};
  const el = {
    textContent: text,
    innerHTML: "",
    className: classes.join(" "),
    hidden: false,
    title: "",
    dataset: { ...dataset },
    children: [],
    classList: {
      contains: (c) => classSet.has(c),
      toggle(c, force) {
        const next = force === undefined ? !classSet.has(c) : Boolean(force);
        if (next) classSet.add(c);
        else classSet.delete(c);
        el.className = [...classSet].join(" ");
        return next;
      },
      add: (c) => { classSet.add(c); el.className = [...classSet].join(" "); },
      remove: (c) => { classSet.delete(c); el.className = [...classSet].join(" "); },
    },
    hasAttribute: (name) => name in attrs,
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    setAttribute(name, val) { attrs[name] = String(val); },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    click() { (listeners.click || []).forEach((fn) => fn()); },
    appendChild(child) {
      this.children.push(child);
      if (child && typeof child.textContent === "string") {
        this.textContent += child.textContent;
      }
    },
    closest: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  return el;
}

function createFadedPage() {
  let now = 0;
  let seq = 0;
  const timers = [];
  const aisRequests = [];
  let pendingFetch = null;
  let nextResponse = null;

  const badge = makeEl({ classes: ["decoder__status", "decoder__status--empty"], text: "UNTUNED" });
  const empty = makeEl({ text: "Not tuned yet — step to marine VHF (162 MHz) and tap Tune." });
  const list = makeEl();
  const freq = makeEl({ classes: ["tune__freq", "tune__freq--pending"] });
  const stepVal = makeEl({ text: "10 MHz" });
  const downBtn = makeEl({ dataset: { fadedDir: "-1" } });
  const upBtn = makeEl({ dataset: { fadedDir: "1" } });
  const stepBtn = makeEl();
  const applyBtn = makeEl();

  const fadedPanel = makeEl({ attrs: { "data-ais-panel": "", "data-ais-faded": "" } });
  fadedPanel.querySelector = (sel) => ({
    "[data-ais-badge]": badge,
    "[data-ais-empty]": empty,
    "[data-ais-list]": list,
    "[data-command]": null,
  })[sel] || null;

  const strip = makeEl({
    attrs: { "data-faded-tune": "ais" },
    dataset: { freqHz: "100000000", stepHz: "10000000", steps: "10000000,1000000,100000" },
  });
  strip.querySelector = (sel) => ({
    "[data-faded-freq]": freq,
    "[data-faded-step-val]": stepVal,
    "[data-faded-step]": stepBtn,
    "[data-faded-apply]": applyBtn,
  })[sel] || null;
  strip.querySelectorAll = (sel) => (sel === "[data-faded-dir]" ? [downBtn, upBtn] : []);

  const doc = {
    querySelectorAll(sel) {
      if (sel === "[data-ais-panel]") return [fadedPanel];
      if (sel === '[data-faded-tune="ais"]') return [strip];
      return [];
    },
    createElement() {
      const child = makeEl();
      child.ownerDocument = doc;
      return child;
    },
  };
  empty.ownerDocument = doc;

  const fetch = (url) => {
    aisRequests.push(url);
    if (nextResponse) {
      const { status, body } = nextResponse;
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
      });
    }
    return new Promise((resolve) => {
      pendingFetch = (status, body) => resolve({
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
      });
    });
  };

  const context = {
    document: doc,
    fetch,
    setTimeout(fn, ms) {
      const id = ++seq;
      timers.push({ id, at: now + (ms || 0), fn });
      return id;
    },
    clearTimeout(id) {
      const idx = timers.findIndex((t) => t.id === id);
      if (idx >= 0) timers.splice(idx, 1);
    },
    addEventListener() {},
    console,
  };
  context.window = context;

  runInNewContext(DECODER_PANEL_SRC, context);
  runInNewContext(AIS_SRC, context);

  return {
    badge,
    empty,
    freq,
    downBtn,
    upBtn,
    stepBtn,
    applyBtn,
    aisRequests,
    setImmediateResponse(status, body) { nextResponse = { status, body }; },
    useDeferredFetch() { nextResponse = null; pendingFetch = null; },
    async resolveFetch(status, body) {
      assert.ok(pendingFetch, "expected an in-flight /api/ais fetch");
      const fn = pendingFetch;
      pendingFetch = null;
      fn(status, body);
      await settle();
      await settle();
    },
    async advance(ms) {
      const until = now + ms;
      for (;;) {
        timers.sort((a, b) => a.at - b.at || a.id - b.id);
        const next = timers[0];
        if (!next || next.at > until) break;
        timers.shift();
        now = next.at;
        next.fn();
        await settle();
      }
      now = until;
      await settle();
    },
    stepTo162() {
      for (let i = 0; i < 6; i++) upBtn.click();
      stepBtn.click();
      upBtn.click();
      upBtn.click();
    },
  };
}

test("the ident is the name, falling back to the MMSI", () => {
  assert.equal(panel.ident({ name: "EVER DIADEM", mmsi: 351759000 }), "EVER DIADEM");
  assert.equal(panel.ident({ mmsi: 477553000 }), "MMSI 477553000");
  assert.equal(panel.ident(null), "MMSI undefined");
});

test("the position is lat, lon to four places, with a note until one arrives", () => {
  assert.equal(panel.position({ lat: 47.58283, lon: -122.34583 }), "47.5828, -122.3458");
  assert.equal(panel.position({ lat: null, lon: null }), "no position yet");
  assert.equal(panel.position({}), "no position yet");
  assert.equal(panel.position(null), "no position yet");
});

test("the motion shows speed and course, each only when reported", () => {
  assert.equal(panel.motion({ sog_kt: 12.3, cog_deg: 51 }), "12.3 kt · 51°");
  assert.equal(panel.motion({ sog_kt: 0 }), "0.0 kt");
  assert.equal(panel.motion({ cog_deg: 118 }), "118°");
  assert.equal(panel.motion({}), "");
  assert.equal(panel.motion(null), "");
});

test("faded AIS tuning refuses off-band, prompts before tuning, and opens on marine VHF 162 MHz", () => {
  const untuned = panel.fadedTuneState(100_000_000, false);
  assert.equal(untuned.tuned, false);
  assert.equal(untuned.badge, "UNTUNED");
  assert.match(untuned.message, /162 MHz/);

  const offBand = panel.fadedTuneState(150_000_000, true);
  assert.equal(offBand.tuned, false);
  assert.equal(offBand.badge, "OFF BAND");
  assert.match(offBand.message, /150\.0 MHz/);
  assert.match(offBand.message, /162 MHz/);

  // Honest dial (N2 fix): never claims "Tuned to <freq>" before the receiver confirms;
  // words it as what the learner chose ("Set to ...").
  for (const hz of [161_900_000, 161_975_000, 162_000_000, 162_025_000, 162_100_000]) {
    const ok = panel.fadedTuneState(hz, true);
    assert.equal(ok.tuned, true);
    assert.equal(ok.confirmed, false);
    assert.equal(ok.badge, "CHECKING");
    assert.match(ok.message, /^Set to 16[12]\.\d MHz/);
    assert.doesNotMatch(ok.message, /\bTuned\b/i);
  }
  assert.equal(panel.inAisBand(161_800_000), false);
  assert.equal(panel.inAisBand(162_200_000), false);

  assert.equal(panel.stepFadedFreq(100_000_000, 1, 10_000_000), 110_000_000);
  assert.equal(panel.stepFadedFreq(24_000_000, -1, 10_000_000), 24_000_000);
  assert.equal(panel.nextFadedStep([10_000_000, 1_000_000, 100_000], 10_000_000), 1_000_000);
  assert.equal(panel.nextFadedStep([10_000_000, 1_000_000, 100_000], 100_000), 10_000_000);
});

test("F1: moving the faded dial after Tune drops back to UNTUNED and unconfirmed", () => {
  const movedOff = panel.onFadedDialMove(164_000_000, true);
  assert.equal(movedOff.active, false);
  assert.equal(movedOff.confirmed, false);
  assert.equal(movedOff.state.badge, "UNTUNED");
  assert.match(movedOff.state.message, /Not tuned yet/);

  const movedIdle = panel.onFadedDialMove(110_000_000, false);
  assert.equal(movedIdle.active, false);
  assert.equal(movedIdle.confirmed, false);
  assert.equal(movedIdle.state, null);
});

test("F2: faded panel scrubs decoder binary name and operator provision hint from refusals", () => {
  const missing = panel.scrubFadedRefusal({
    cause: "missing_tool",
    message: "rtl_ais-sigint101 is not installed.",
    next_step: "Run deploy/provision.sh",
  });
  assert.equal(missing.cause, "missing_tool");
  assert.doesNotMatch(missing.message, /rtl[_\s-]?ais|aisdecoder/i);
  assert.doesNotMatch(missing.next_step, /rtl[_\s-]?ais|provision\.sh/i);
  assert.match(missing.next_step, /Field card/i);

  const stopped = panel.scrubFadedRefusal({
    cause: "stopped",
    reason: "rtl_ais exited unexpectedly",
    retry_in: 4,
  });
  assert.equal(stopped.cause, "stopped");
  assert.equal(stopped.retry_in, 4);
  assert.doesNotMatch(stopped.reason, /rtl[_\s-]?ais|aisdecoder/i);
});

test("F1a: moving the dial after a rejected off-band Tune returns to UNTUNED with zero AIS requests", () => {
  const page = createFadedPage();
  // Tap Tune at 100.0 MHz -> rejected OFF BAND.
  page.applyBtn.click();
  assert.equal(page.badge.textContent, "OFF BAND");
  assert.match(page.empty.textContent, /100\.0 MHz is outside/);
  assert.equal(page.aisRequests.length, 0);

  // Step dial to 162.0 MHz without tapping Tune -> clears stale OFF BAND and stays UNTUNED.
  page.stepTo162();
  assert.equal(page.badge.textContent, "UNTUNED");
  assert.match(page.empty.textContent, /Not tuned yet/);
  assert.doesNotMatch(page.empty.textContent, /100\.0 MHz|outside/i);
  assert.equal(page.freq.classList.contains("tune__freq--pending"), true);
  assert.equal(page.aisRequests.length, 0);
});

test("F1b: dial stays pending through starting and clears only on running", async () => {
  assert.equal(typeof panel.isAisReceiverConfirmed, "function");
  assert.equal(panel.isAisReceiverConfirmed({ available: true, state: "starting" }), false);
  assert.equal(panel.isAisReceiverConfirmed({ available: true, state: "running" }), true);
  assert.equal(panel.isAisReceiverConfirmed({ available: false, state: "stopped" }), false);

  // Path 1: deferred starting -> running.
  const page = createFadedPage();
  page.useDeferredFetch();
  page.stepTo162();
  page.applyBtn.click();
  assert.equal(page.badge.textContent, "CHECKING");
  assert.equal(page.freq.classList.contains("tune__freq--pending"), true);

  await page.resolveFetch(200, { available: true, state: "starting", vessels: [] });
  assert.equal(page.badge.textContent, "STARTING");
  assert.equal(page.freq.classList.contains("tune__freq--pending"), true,
    "dial must stay pending while receiver is settling (state === 'starting')");

  await page.advance(1500);
  await page.resolveFetch(200, { available: true, state: "running", vessels: [] });
  assert.equal(page.badge.textContent, "LIVE");
  assert.equal(page.freq.classList.contains("tune__freq--pending"), false,
    "dial clears pending once receiver reports state === 'running'");

  // Path 2: deferred starting -> stopped.
  const page2 = createFadedPage();
  page2.useDeferredFetch();
  page2.stepTo162();
  page2.applyBtn.click();
  await page2.resolveFetch(200, { available: true, state: "starting", vessels: [] });
  assert.equal(page2.badge.textContent, "STARTING");
  assert.equal(page2.freq.classList.contains("tune__freq--pending"), true);

  await page2.advance(1500);
  await page2.resolveFetch(503, {
    available: false,
    cause: "stopped",
    state: "stopped",
    reason: "rtl_ais-sigint101 sent an unreadable datagram (truncated); run deploy/provision.sh",
    retry_in: 4,
  });
  assert.equal(page2.badge.textContent, "STOPPED");
  assert.equal(page2.freq.classList.contains("tune__freq--pending"), true);
  assert.match(page2.empty.textContent, /retrying in 4 s/);
  assert.doesNotMatch(page2.empty.textContent, /provision\.sh|rtl[_\s-]?ais|aisdecoder/i);
});

test("F2: faded panel shows learner-facing copy for capability timeout, rejected datagram, missing-tool, and no-radio", async () => {
  const cases = [
    {
      name: "capability timeout",
      payload: {
        available: false,
        cause: "unavailable",
        reason: "rtl_ais-sigint101 --sigint101-capability timed out; run deploy/provision.sh",
      },
      badge: "UNAVAILABLE",
      mustMatch: /unavailable/i,
    },
    {
      name: "rejected datagram",
      payload: {
        available: false,
        cause: "stopped",
        state: "stopped",
        reason: "rtl_ais-sigint101 sent an unreadable datagram (truncated); run deploy/provision.sh",
        retry_in: 4,
      },
      badge: "STOPPED",
      mustMatch: /retrying in 4 s/,
    },
    {
      name: "missing tool",
      payload: {
        available: false,
        cause: "missing_tool",
        reason: "rtl_ais-sigint101 is not installed, so SIGINT 101 cannot decode AIS",
        message: "rtl_ais-sigint101 is not installed.",
        next_step: "Run deploy/provision.sh",
      },
      badge: "TOOL MISSING",
      mustMatch: /Field card/i,
    },
    {
      name: "no radio",
      payload: {
        available: false,
        cause: "no_radio",
        reason: "no RTL-SDR on the USB bus — SIGINT 101 will not fake a vessel",
        message: "No RTL-SDR on the USB bus.",
        next_step: "Plug in the RTL-SDR.",
      },
      badge: "NO RADIO",
      mustMatch: /RTL-SDR/i,
    },
  ];

  for (const c of cases) {
    const page = createFadedPage();
    page.setImmediateResponse(503, c.payload);
    page.stepTo162();
    page.applyBtn.click();
    await settle();
    await settle();
    assert.equal(page.badge.textContent, c.badge, `${c.name}: badge`);
    assert.equal(page.freq.classList.contains("tune__freq--pending"), true, `${c.name}: pending`);
    assert.match(page.empty.textContent, c.mustMatch, `${c.name}: learner copy`);
    assert.doesNotMatch(
      page.empty.textContent,
      /provision\.sh|rtl[_\s-]?ais|aisdecoder|--sigint101-capability/i,
      `${c.name}: must not leak provisioning command or binary name`
    );
  }
});
