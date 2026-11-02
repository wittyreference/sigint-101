// ABOUTME: Tests the rack's own wiring (operate.js): the step key, the decoder frequency override's
// ABOUTME: tap coalescing and caption, and the reception log line. The DOM wiring is render-checked.

const test = require("node:test");
const assert = require("node:assert");
const rack = require("../sigint101/static/operate.js");

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

test("the pager caption says the text is withheld inside the paging band only", () => {
  const strip = { bandName: "the VHF, UHF and 900 MHz paging bands",
                  withheldLow: 929000000, withheldHigh: 932000000 };
  assert.equal(rack.tuneCaption(929612500, strip), "framing only · text withheld");
  assert.equal(rack.tuneCaption(152007500, strip), "the VHF, UHF and 900 MHz paging bands");
  assert.equal(rack.tuneCaption(144390000, { bandName: "the 2 m band" }), "the 2 m band");
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
