// ABOUTME: Tests the provenance panel's one pure decision — the reference-vs-baseline verdict that
// ABOUTME: tells a deaf radio from a quiet band. The DOM/polling is deliberately not exercised here.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/prov.js");

test("no reference and no temperature reads as NO REFERENCE, never a guessed verdict", () => {
  const v = panel.provenanceVerdict({ ref_db: null, soc_temp_c: null }, { has_baseline: false });
  assert.match(v.text, /NO REFERENCE/);
  assert.equal(v.kind, "na");
});

// The Pi's own chip temperature reads with no radio attached, so it is no evidence of one. With no
// reference reading (radio unplugged, sweep off the FM band, the first poll before a sweep row) the
// panel has nothing to judge the radio by, whatever the baseline says.
test("a saved baseline with no reference reading is never HEALTHY, whatever the temperature", () => {
  const v = panel.provenanceVerdict(
    { ref_db: null, soc_temp_c: 47.2 },
    { has_baseline: true, degraded: false }
  );
  assert.doesNotMatch(v.text, /HEALTHY/);
  assert.match(v.text, /NO REFERENCE/);
  assert.equal(v.kind, "na");
});

test("no reference and no baseline reads as NO REFERENCE, not a call to capture one", () => {
  // Capture is refused without a reference reading, so asking for one points at a dead key.
  const v = panel.provenanceVerdict({ ref_db: null, soc_temp_c: 47 }, { has_baseline: false });
  assert.match(v.text, /NO REFERENCE/);
  assert.equal(v.kind, "na");
});

test("a missing reference is judged before the baseline, so no stale flag becomes a verdict", () => {
  const v = panel.provenanceVerdict({ soc_temp_c: 47 }, { has_baseline: true, degraded: true });
  assert.match(v.text, /NO REFERENCE/);
  assert.equal(v.kind, "na");
});

test("with a reference reading, an unreadable temperature changes no verdict", () => {
  const cases = [
    [{ has_baseline: false }, /NO BASELINE/, "na"],
    [{ has_baseline: true, degraded: false }, /HEALTHY/, "ok"],
    [{ has_baseline: true, degraded: true }, /DEGRADED/, "bad"],
  ];
  for (const [b, text, kind] of cases) {
    const v = panel.provenanceVerdict({ ref_db: -31.8, soc_temp_c: null }, b);
    assert.match(v.text, text);
    assert.equal(v.kind, kind);
  }
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
