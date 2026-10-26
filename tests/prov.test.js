// ABOUTME: Tests the provenance panel's one pure decision — the reference-vs-baseline verdict that
// ABOUTME: tells a deaf radio from a quiet band. The DOM/polling is deliberately not exercised here.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/prov.js");

test("no reference and no temperature reads as NO RADIO, never a guessed verdict", () => {
  const v = panel.provenanceVerdict({ ref_db: null, soc_temp_c: null }, { has_baseline: false });
  assert.equal(v.text, "NO RADIO");
  assert.equal(v.kind, "na");
});

test("a radio with a reading but no baseline asks for one rather than judging the radio", () => {
  const v = panel.provenanceVerdict({ ref_db: -32.1, soc_temp_c: 47 }, { has_baseline: false });
  assert.match(v.text, /NO BASELINE/);
  assert.equal(v.kind, "na");
});

test("a reference holding at the baseline reads HEALTHY", () => {
  const v = panel.provenanceVerdict(
    { ref_db: -31.8, soc_temp_c: 47 },
    { has_baseline: true, degraded: false }
  );
  assert.match(v.text, /HEALTHY/);
  assert.equal(v.kind, "ok");
});

test("a reference that has dropped past the threshold reads DEGRADED — the deaf-radio signal", () => {
  const v = panel.provenanceVerdict(
    { ref_db: -49.0, soc_temp_c: 47 },
    { has_baseline: true, degraded: true }
  );
  assert.match(v.text, /DEGRADED/);
  assert.equal(v.kind, "bad");
});
