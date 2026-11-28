// ABOUTME: Tests the capture panel's request ordering and confirmed status display.
// ABOUTME: Holds browser replies independently so the tests use no clock or radio.

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");

const tune = { hz: 433921017, gain: "19.7" };
const idle = { state: "idle", confirmed: tune };
const recording = { state: "recording", run_id: "run", confirmed: tune, tune };
const settled = () => new Promise(resolve => setImmediate(resolve));

function view(initial = idle) {
  const nodes = Object.fromEntries(["start", "stop", "state", "tune", "saved"].map(key =>
    [key, { addEventListener(_type, listener) { this.tap = listener; } }]));
  const waiting = [];
  const timers = [];
  runInNewContext(readFileSync(require.resolve("../sigint101/static/capture.js"), "utf8"), {
    document: {
      querySelector(selector) {
        return selector === "#capture-status" ? { textContent: JSON.stringify(initial) }
          : { querySelector: attr => nodes[attr.slice("[data-capture-".length, -1)] };
      },
    },
    window: { addEventListener() {} },
    AbortSignal,
    fetch(url, options = {}) {
      return new Promise(resolve => waiting.push({
        url, options, reply: (body, ok = true) => resolve({ ok, json: async () => body }),
      }));
    },
    setInterval(listener) { timers.push(listener); },
  });
  return { nodes, waiting, poll: timers[0], next: () => waiting.shift() };
}

test("opening capture sends only a read and Record posts the exact dial confirmation", async () => {
  const p = view();
  assert.equal(p.waiting.length, 0);
  p.poll();
  const status = p.next();
  assert.equal(status.url, "/api/capture");
  assert.equal(status.options.method, undefined);
  status.reply(idle);
  await settled();
  assert.equal(p.nodes.state.textContent, "Ready to record");
  p.nodes.start.tap();
  const start = p.next();
  assert.equal(start.url, "/api/capture/start");
  assert.equal(start.options.method, "POST");
  assert.deepEqual(JSON.parse(start.options.body), tune);
  start.reply(recording);
  await settled();
  assert.equal(p.nodes.state.textContent, "Recording…");
});

test("Stop during an opening fences both the late Start reply and an older poll", async () => {
  const p = view();
  p.poll();
  const poll = p.next();
  p.nodes.start.tap();
  const start = p.next();
  assert.equal(p.nodes.stop.disabled, false);
  p.nodes.stop.tap();
  const stop = p.next();
  assert.equal(stop.url, "/api/capture/stop");
  stop.reply({ ...recording, state: "stopped" });
  start.reply(recording);
  poll.reply(idle);
  await settled();
  assert.equal(p.nodes.state.textContent, "Capture stopped");
});

test("a saved result replaces the refusal for a run that had been busy", async () => {
  const p = view();
  p.nodes.start.tap();
  p.next().reply({ ...recording, reason: "A capture is already in progress" }, false);
  await settled();
  assert.equal(p.nodes.state.textContent, "A capture is already in progress");
  p.poll();
  p.next().reply({ ...recording, state: "saved", last_saved: { id: "clip" } });
  await settled();
  assert.equal(p.nodes.state.textContent, "Capture saved");
  assert.equal(p.nodes.saved.hidden, false);
});

test("a save can be discarded until the commit is admitted", () => {
  const state = { ...recording, state: "saving" };
  const saving = view(state).nodes;
  assert.equal(saving.stop.textContent, "Stop and discard");
  assert.equal(saving.stop.disabled, false);
  const committing = view({ ...state, finishing: true }).nodes;
  assert.equal(committing.state.textContent, "Finishing save…");
  assert.equal(committing.stop.disabled, true);
});
