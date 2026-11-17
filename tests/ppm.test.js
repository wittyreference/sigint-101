// ABOUTME: Tests the ppm panel's one pure decision — how a measurement result reads to the learner:
// ABOUTME: the signed ppm with its resolution, or an honest refusal when no pilot was found.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/ppm.js");

test("a measured ppm reads with its sign and leads as ok", () => {
  const v = panel.ppmResultText({ ppm: 164.3, pilot_ratio_db: 22.0, resolution_ppm: 0.5 });
  assert.match(v.text, /\+164\.3 ppm/);
  assert.equal(v.kind, "ok");
  assert.match(v.detail, /22 dB/);
});

test("a negative ppm keeps its minus sign", () => {
  const v = panel.ppmResultText({ ppm: -2.1, pilot_ratio_db: 18 });
  assert.match(v.text, /-2\.1 ppm/);
});

// The resolution is 1/T, the width of one spectral bin for the capture's length: 6.579 ppm at the
// console's 8 s. It says how finely the capture reads, not how far the reading can be wrong, so it
// is labelled with its unit and never printed as a ± on the reading.
test("the capture's resolution reads as resolution in ppm, to one decimal, with no ± on it", () => {
  const v = panel.ppmResultText({ ppm: 164.53, pilot_ratio_db: 31.25, resolution_ppm: 6.579 });
  assert.match(v.detail, /resolution 6\.6 ppm/);
  assert.doesNotMatch(v.detail, /±\s*6\.(6|579)\b/);
  const longer = panel.ppmResultText({ ppm: 164.53, pilot_ratio_db: 31.25, resolution_ppm: 2.632 });
  assert.match(longer.detail, /resolution 2\.6 ppm/);
});

// The reference has a tolerance of its own: 47 CFR 73.322(a)(2) sets the pilot at 19 kHz ±2 Hz,
// and 2 Hz in 19 000 Hz is 105 ppm, the same order as the error the panel measures.
test("every reading says what the rule holds the pilot to: ±2 Hz, which is ±105 ppm", () => {
  const rulePpm = Math.round((2 / 19000) * 1e6);
  const rule = new RegExp(`pilot held to ±2 Hz \\(±${rulePpm} ppm\\) by rule`);
  const v = panel.ppmResultText({ ppm: 164.53, pilot_ratio_db: 31.25, resolution_ppm: 6.579 });
  assert.match(v.detail, rule);
  assert.match(panel.ppmResultText({ ppm: -2.1, pilot_ratio_db: 18 }).detail, rule);
});

test("no pilot reads as an honest refusal, never a guessed number", () => {
  const v = panel.ppmResultText({ ppm: null, reason: "no 19 kHz pilot here" });
  assert.match(v.text, /no clear pilot/);
  assert.equal(v.kind, "na");
  assert.match(v.detail, /pilot/);
});

// Which station the Measure button captures. The waterfall puts data-tuned-hz on the dial only for
// a frequency the radio has confirmed; the dial's printed text is never parsed, because it can be
// a pending target the radio is not on.
test("the measure captures the frequency the radio confirmed, to the hertz", () => {
  assert.equal(panel.measureHz("91515625", "98100000"), 91515625);
});

test("with nothing confirmed the measure falls back to the panel's own station", () => {
  assert.equal(panel.measureHz(null, "98100000"), 98100000);
  assert.equal(panel.measureHz("", "98100000"), 98100000);
  assert.equal(panel.measureHz("junk", null), 98100000);
});
