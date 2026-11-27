// ABOUTME: Tests the shared decoder-panel states, command text, and waterfall polling.
// ABOUTME: Uses node's built-in runner with a minimal page and server for waterfall state changes.

const test = require("node:test");
const assert = require("node:assert");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const panel = require("../sigint101/static/decoder-panel.js");

test("a tool that died on its own reads STOPPED, never LIVE or NO RADIO", () => {
  const down = panel.downState(
    { available: false, state: "stopped", reason: "rtl_433 stopped (exit status 1)", retry_in: 3.2 },
    "NO RADIO", "no radio to decode");
  assert.equal(down.badge, "STOPPED");
  assert.ok(down.cls.includes("decoder__status--stopped"));
  assert.equal(down.reason, "rtl_433 stopped (exit status 1) · retrying in 4 s");
});

test("a stopped tool past its back-off says it is retrying now", () => {
  const down = panel.downState(
    { available: false, state: "stopped", reason: "multimon-ng stopped (killed by SIGKILL)",
      retry_in: 0 },
    "NO RADIO", "x");
  assert.equal(down.reason, "multimon-ng stopped (killed by SIGKILL) · retrying now");
});

test("an unclassified refusal keeps its reason under a neutral badge", () => {
  const down = panel.downState({ available: false, reason: "Orbic unreachable" });
  assert.equal(down.badge, "UNAVAILABLE");
  assert.ok(down.cls.includes("decoder__status--empty"));
  assert.equal(down.reason, "Orbic unreachable");
});

test("a refusal with no body has a neutral explanation", () => {
  const down = panel.downState(null);
  assert.equal(down.badge, "UNAVAILABLE");
  assert.equal(down.reason, "Receiver unavailable.");
});

test("a running tool reads LIVE only once it has settled", () => {
  assert.deepEqual(panel.liveBadge({ available: true, state: "starting" }),
    { text: "STARTING", cls: "decoder__status" });
  assert.deepEqual(panel.liveBadge({ available: true, state: "running" }),
    { text: "LIVE", cls: "decoder__status decoder__status--live" });
  assert.deepEqual(panel.liveBadge({ available: true }),
    { text: "LIVE", cls: "decoder__status decoder__status--live" });
});

test("the command text is exactly what the console sent, or nothing", () => {
  assert.equal(panel.commandText("rtl_fm -f 144390000 | multimon-ng -a AFSK1200"),
    "rtl_fm -f 144390000 | multimon-ng -a AFSK1200");
  assert.equal(panel.commandText(null), "");
  assert.equal(panel.commandText(undefined), "");
  assert.equal(panel.commandText(["rtl_433"]), "");   // only ever a string from the API
});

test("the refusal badge is decided by the API, with a neutral default", () => {
  assert.equal(panel.downState({ cause: "no_radio" }, "NO ORBIC").badge, "NO RADIO");
  assert.equal(panel.downState({ cause: "missing_tool" }, "NO RADIO").badge, "TOOL MISSING");
  assert.equal(panel.downState({ cause: "unavailable" }, "NO RADIO").badge, "UNAVAILABLE");
});

test("a quiet receiver waits until it is running to say it is listening", () => {
  assert.equal(panel.quietText({ state: "starting" }, "no messages yet"), "Checking receiver…");
  assert.equal(panel.quietText({ state: "running" }, "no messages yet"),
    "Listening · no messages yet");
});

test("the panel includes the API's next step and preserves the stopped countdown", () => {
  const missing = {
    cause: "missing_tool", reason: "satdump is not installed",
    next_step: "Run deploy/provision.sh --with-optional",
  };
  assert.equal(panel.refusalText(panel.downState(missing)),
    "satdump is not installed · Run deploy/provision.sh --with-optional");
  const stopped = { cause: "stopped", reason: "tool exited", retry_in: 1.2 };
  assert.equal(panel.refusalText(panel.downState(stopped)), "tool exited · retrying in 2 s");
});

test("the waterfall follows each refusal and redraws the last measured pass on recovery", async () => {
  const badge = {};
  const reason = { firstElementChild: {} };
  const live = {
    freq_low: 88e6, freq_high: 108e6,
    rows: [{ ts: 1, freq_low: 88e6, freq_high: 108e6, dbm: [-80, -40] }],
  };
  const replies = [
    [200, live],
    [503, { cause: "missing_tool", reason: "rtl_power missing", next_step: "Run deploy/provision.sh" }],
    [503, { cause: "stopped", reason: "sweep stopped", retry_in: 1.2 }],
    [200, live],
  ];
  let painted = 0;
  const canvas = {
    getContext: () => ({ fillRect() {}, drawImage() { painted++; } }),
    getAttribute: () => null,
    closest: () => null,
    addEventListener() {},
  };
  const wraps = [{ querySelector: key => ({
    "canvas.waterfall": canvas, ".badge": badge, "[data-wf-reason]": reason,
  })[key], closest: () => null }];
  const scheduled = [];
  runInNewContext(readFileSync(require.resolve("../sigint101/static/waterfall.js"), "utf8"), {
    document: { querySelectorAll: key => key === ".waterfall-wrap" ? wraps : [],
                querySelector: () => null },
    window: { DecoderPanel: panel },
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    setTimeout: next => scheduled.push(next),
    fetch: async () => {
      const [status, body] = replies.shift();
      return { status, json: async () => body };
    },
  });
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const poll = async () => {
    assert.equal(scheduled.length, 1);
    scheduled.shift()();
    await settle();
  };

  assert.equal(badge.textContent, "checking");
  await settle();
  assert.equal(painted, 1);
  await poll();
  assert.equal(badge.textContent, "TOOL MISSING");
  assert.equal(reason.firstElementChild.textContent, "rtl_power missing · Run deploy/provision.sh");
  await poll();
  assert.equal(badge.textContent, "STOPPED");
  assert.equal(reason.firstElementChild.textContent, "sweep stopped · retrying in 2 s");
  await poll();
  assert.equal(badge.textContent, "live · 1");
  assert.equal(reason.hidden, true);
  assert.equal(painted, 2);
});
