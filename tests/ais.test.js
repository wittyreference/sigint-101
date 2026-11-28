// ABOUTME: Tests the AIS panel's pure helpers — the vessel ident, its position, and its motion.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/ais.js");

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
