// ABOUTME: Tests the rack's own wiring (operate.js): the picker's place post, the step key, the
// ABOUTME: decoder override's tap coalescing and caption, and the log line. The rest is render-checked.

const test = require("node:test");
const assert = require("node:assert");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const rack = require("../sigint101/static/operate.js");

// The rack page as operate.js sees it: a picker that keeps its click listener, no bench widgets.
function openRack(fetch) {
  const calls = [];
  const listeners = {};
  const current = { scrollIntoView: options => calls.push(options.block) };
  const picker = {
    querySelector: name => name === ".rack__pick--on" ? current : null,
    addEventListener: (type, listener) => { listeners[type] = listener; },
  };
  const bench = { querySelector: () => null };
  runInNewContext(readFileSync(require.resolve("../sigint101/static/operate.js"), "utf8"), {
    document: { querySelector: name => ({
      ".rack__bench": bench, ".rack__picker": picker, ".rack__pick[aria-current]": current,
    })[name] },
    fetch: fetch || (() => Promise.resolve()),
  });
  return { calls, listeners };
}

// A tap inside a picker entry: the event's target is a child of the entry's link.
function tapOn(key) {
  const link = key === null ? null : { getAttribute: name => name === "data-rack-pick" ? key : null };
  return { target: { closest: selector => selector === "[data-rack-pick]" ? link : null } };
}

test("opening the rack centres the current instrument within its picker", () => {
  assert.deepEqual(openRack().calls, ["center"]);
});

test("opening the rack records nothing: only a pick posts the rack place", () => {
  const sent = [];
  openRack((url, init) => { sent.push([url, init]); return Promise.resolve(); });
  assert.deepEqual(sent, []);
});

test("picking an instrument posts it as the rack place, surviving the page change", () => {
  const sent = [];
  const page = openRack((url, init) => { sent.push([url, init]); return Promise.resolve(); });
  page.listeners.click(tapOn("aprs"));
  assert.equal(sent.length, 1);
  const [url, init] = sent[0];
  assert.equal(url, "/api/rack/place");
  assert.equal(init.method, "POST");
  assert.equal(init.keepalive, true, "the post must outlive the navigation the tap starts");
  assert.equal(init.headers["content-type"], "application/json");
  assert.deepEqual(JSON.parse(init.body), { instrument: "aprs" });
});

test("a tap in the picker outside an entry posts nothing", () => {
  const sent = [];
  const page = openRack((url, init) => { sent.push([url, init]); return Promise.resolve(); });
  page.listeners.click(tapOn(null));
  assert.deepEqual(sent, []);
});

test("a failed place post is swallowed, so the tap still navigates", async () => {
  const page = openRack(() => Promise.reject(new Error("offline")));
  page.listeners.click(tapOn("ism"));
  await new Promise(resolve => setImmediate(resolve));
});

test("the place request names the instrument picked", () => {
  const request = rack.placeRequest("lora");
  assert.equal(request.url, "/api/rack/place");
  assert.deepEqual(JSON.parse(request.init.body), { instrument: "lora" });
});

// --- the step key -------------------------------------------------------------------------------

test("a step reads in kHz or MHz the way the server rendered it", () => {
  assert.equal(rack.formatStep(100000), "100 kHz");
  assert.equal(rack.formatStep(12500), "12.5 kHz");
  assert.equal(rack.formatStep(6250), "6.25 kHz");
  assert.equal(rack.formatStep(1000000), "1 MHz");
  assert.equal(rack.formatStep(10000000), "10 MHz");
});

test("the step key walks the instrument's steps and wraps back to the first", () => {
  const steps = rack.parseSteps("100000,1000000,10000000");
  assert.deepEqual(steps, [100000, 1000000, 10000000]);
  assert.equal(rack.nextStep(steps, 100000), 1000000);
  assert.equal(rack.nextStep(steps, 10000000), 100000);
  assert.equal(rack.nextStep(steps, 5), 100000, "a step not in the list starts the walk over");
});

// --- the decoder frequency override --------------------------------------------------------------

function harness(t, start) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const sent = [];
  const pending = [];
  const results = [];
  let resolveSend = null;
  const state = { hz: start, step: 25000 };
  const stepper = rack.makeStepper({
    delayMs: 400,
    current: () => state.hz,
    stepHz: () => state.step,
    send: (hz) => { sent.push(hz); return new Promise((res) => { resolveSend = res; }); },
    onPending: (hz) => pending.push(hz),
    onResult: (res) => { results.push(res); if (res.tuned) state.hz = res.freq_hz; },
  });
  const settle = async (res) => { resolveSend(res); await new Promise((r) => setImmediate(r)); };
  return { stepper, sent, pending, results, settle, state };
}

test("quick taps become one retune, counted from the frequency the console confirmed", (t) => {
  const h = harness(t, 144390000);
  h.stepper.step(1); h.stepper.step(1);
  t.mock.timers.tick(399);
  assert.equal(h.sent.length, 0, "sent before the taps went quiet");
  t.mock.timers.tick(1);
  assert.deepEqual(h.sent, [144440000]);
  assert.deepEqual(h.pending, [144415000, 144440000]);
});

test("taps during a request go out after it, from the frequency it confirmed", async (t) => {
  const h = harness(t, 144390000);
  h.stepper.step(-1);
  t.mock.timers.tick(400);
  h.stepper.step(-1);
  t.mock.timers.tick(400);
  assert.equal(h.sent.length, 1, "two requests in flight at once");
  await h.settle({ tuned: true, freq_hz: 144365000 });
  t.mock.timers.tick(400);
  assert.deepEqual(h.sent, [144365000, 144340000]);
});

test("a refused retune is reported and the dial stays where the console has it", async (t) => {
  const h = harness(t, 144390000);
  h.stepper.step(400);
  t.mock.timers.tick(400);
  await h.settle({ tuned: false, reason: "APRS tunes 144-148 MHz (the 2 m band)" });
  assert.equal(h.results[0].tuned, false);
  assert.equal(h.state.hz, 144390000);
  assert.equal(h.pending[h.pending.length - 1], null);
});

test("the pager caption says the text is withheld, wherever the dial is", () => {
  // No frequency goes in: the pager shows framing only on every one, so the caption cannot vary.
  const pager = { bandName: "the VHF, UHF and 900 MHz paging bands", textWithheld: true };
  assert.equal(rack.tuneCaption(pager), "framing only · text withheld");
  assert.equal(rack.tuneCaption({ bandName: "the 2 m band", textWithheld: false }),
               "the 2 m band");
});

test("a decoder dial reads MHz, the pager to the 100 Hz its channels need", () => {
  assert.equal(rack.formatDecoderMHz(144390000, false), "144.390");
  assert.equal(rack.formatDecoderMHz(929612500, true), "929.6125");
});

// --- the reception log line -----------------------------------------------------------------------

test("a reception reads as time, tool, frequency and what was heard", () => {
  const ts = 1793151825.5;
  const d = new Date(ts * 1000);
  const hhmmss = [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0")).join(":");
  const line = rack.receptionLine({ ts, payload: { tool: "rtl_433", freq_hz: 433920000,
                                                   summary: "Acurite-Tower · 18.0 °C" } });
  assert.equal(line, hhmmss + " · rtl_433 · 433.920 MHz · Acurite-Tower · 18.0 °C");
});

test("a two-channel or channel-less reception still reads cleanly", () => {
  assert.equal(rack.freqLabel([161975000, 162025000]), "161.975/162.025 MHz");
  assert.equal(rack.freqLabel(null), "");
  const line = rack.receptionLine({ ts: 0, payload: { tool: "meshtasticd", freq_hz: null,
                                                      summary: "from !abcd" } });
  assert.ok(line.endsWith(" · meshtasticd · from !abcd"), line);
});

test("the log asks for this instrument's newest reception, or anyone's on a sweep view", () => {
  assert.equal(rack.logQuery("aprs"), "/api/receptions?limit=1&instrument=aprs");
  assert.equal(rack.logQuery(""), "/api/receptions?limit=1");
});

test("with nothing logged the line says so plainly", () => {
  assert.equal(rack.logText({ receptions: [], error: null }), "no receptions logged yet");
  assert.equal(rack.logText({ receptions: [], error: "disk full" }), "disk full");
});

// C81 round 4: a decoder tune is a learner action, so it carries the action clock's stamp.
test("a decoder tune carries the learner's action stamp beside the frequency", () => {
  const request = rack.decoderTuneRequest("aprs", 144390000, { epoch: "5eed", at: 4321 });
  assert.equal(request.url, "/api/aprs/tune");
  assert.equal(request.init.method, "POST");
  assert.deepEqual(JSON.parse(request.init.body), { freq_hz: 144390000, epoch: "5eed", at: 4321 });
  assert.deepEqual(JSON.parse(rack.decoderTuneRequest("aprs", 1, null).init.body), { freq_hz: 1 });
});

// C81 round 5 (Codex D-132 F2): a step is stamped when the learner taps, and the stamp rides
// through the debounce and any request in flight, so a tap made before a newer lesson opened is
// still seen as older by the console however late it goes out.
test("a decoder step carries the stamp of its tap through the debounce and a request in flight",
     async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let clockAt = 2000;
  const sent = [];
  let resolveSend = null;
  const stepper = rack.makeStepper({
    delayMs: 400,
    current: () => 144390000,
    stepHz: () => 25000,
    stamp: () => ({ epoch: "5eed", at: clockAt }),
    send: (hz, stamp) => { sent.push([hz, stamp]); return new Promise((r) => { resolveSend = r; }); },
    onPending: () => {},
    onResult: () => {},
  });
  stepper.step(1);                     // tapped at 2,000
  t.mock.timers.tick(400);
  clockAt = 2005;
  stepper.step(1);                     // tapped at 2,005, while the first is in flight
  clockAt = 2255;                      // a newer lesson's restore (2,105) ran meanwhile
  t.mock.timers.tick(400);
  resolveSend({ tuned: true, freq_hz: 144415000 });
  await new Promise((r) => setImmediate(r));
  t.mock.timers.tick(400);
  assert.deepEqual(sent.map(([, stamp]) => stamp.at), [2000, 2005]);
  const body = JSON.parse(rack.decoderTuneRequest("aprs", sent[1][0], sent[1][1]).init.body);
  assert.equal(body.at, 2005, "the request went out with its send time, not its tap time");
});
