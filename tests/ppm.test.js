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

test("no pilot reads as an honest refusal, never a guessed number", () => {
  const v = panel.ppmResultText({ ppm: null, reason: "no 19 kHz pilot here" });
  assert.match(v.text, /no clear pilot/);
  assert.equal(v.kind, "na");
  assert.match(v.detail, /pilot/);
});
