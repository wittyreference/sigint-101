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

  for (const hz of [161_975_000, 162_000_000, 162_025_000]) {
    const ok = panel.fadedTuneState(hz, true);
    assert.equal(ok.tuned, true);
    assert.equal(ok.badge, "CHECKING");
  }
  assert.equal(panel.inAisBand(161_800_000), false);
  assert.equal(panel.inAisBand(162_200_000), false);

  assert.equal(panel.stepFadedFreq(100_000_000, 1, 10_000_000), 110_000_000);
  assert.equal(panel.stepFadedFreq(24_000_000, -1, 10_000_000), 24_000_000);
  assert.equal(panel.nextFadedStep([10_000_000, 1_000_000, 100_000], 10_000_000), 1_000_000);
  assert.equal(panel.nextFadedStep([10_000_000, 1_000_000, 100_000], 100_000), 10_000_000);
});
