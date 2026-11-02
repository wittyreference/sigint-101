// ABOUTME: Tests the shared decoder-panel helpers: a stopped tool's badge and line, the command text.
// ABOUTME: Uses node's built-in runner; the DOM wiring (strip fill, tap to expand) is render-checked.

const test = require("node:test");
const assert = require("node:assert");
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

test("any other refusal keeps the panel's own wording", () => {
  const down = panel.downState(
    { available: false, reason: "no RTL-SDR on the USB bus" }, "NO ORBIC", "fallback");
  assert.equal(down.badge, "NO ORBIC");
  assert.ok(down.cls.includes("decoder__status--empty"));
  assert.equal(down.reason, "no RTL-SDR on the USB bus");
});

test("a refusal with no body falls back to the panel's own reason", () => {
  const down = panel.downState(null, "NO RADIO", "no radio to decode APRS");
  assert.equal(down.badge, "NO RADIO");
  assert.equal(down.reason, "no radio to decode APRS");
});

test("the command text is exactly what the console sent, or nothing", () => {
  assert.equal(panel.commandText("rtl_fm -f 144390000 | multimon-ng -a AFSK1200"),
    "rtl_fm -f 144390000 | multimon-ng -a AFSK1200");
  assert.equal(panel.commandText(null), "");
  assert.equal(panel.commandText(undefined), "");
  assert.equal(panel.commandText(["rtl_433"]), "");   // only ever a string from the API
});
