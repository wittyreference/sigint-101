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
test("a saved baseline with no reference reading never reads REFERENCE HOLDS, whatever the temperature", () => {
  const v = panel.provenanceVerdict(
    { ref_db: null, soc_temp_c: 47.2 },
    { has_baseline: true, degraded: false }
  );
  assert.doesNotMatch(v.text, /REFERENCE HOLDS/);
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
    [{ has_baseline: true, degraded: false }, /REFERENCE HOLDS/, "ok"],
    [{ has_baseline: true, degraded: true }, /REFERENCE DROPPED/, "bad"],
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

// The verdict names what it checked: one station's level against its own baseline. The reference
// counts as holding until it falls more than 6 dB under the baseline median (provenance.py,
// compare_to_baseline), so a reference that holds may have lost up to 6 dB there, and it shows
// that the radio still hears that station, not that the radio is healthy.
test("a reference holding at its baseline reads REFERENCE HOLDS, naming what it checked", () => {
  const v = panel.provenanceVerdict(
    { ref_db: -31.8, soc_temp_c: 47 },
    { has_baseline: true, degraded: false }
  );
  assert.equal(v.text, "✓ REFERENCE HOLDS");
  assert.equal(v.kind, "ok");
});

// Past the threshold the panel knows only that the reference dropped: anything from just over
// 6 dB down, about a quarter of its power and still heard, to gone. A 6 dB drop is not deafness,
// so the verdict says what dropped and leaves the cause to the learner.
test("a reference that has dropped past the threshold reads REFERENCE DROPPED, not deafness", () => {
  const v = panel.provenanceVerdict(
    { ref_db: -49.0, soc_temp_c: 47 },
    { has_baseline: true, degraded: true }
  );
  assert.equal(v.text, "▼ REFERENCE DROPPED");
  assert.equal(v.kind, "bad");
});

// A baseline counts only for the set-up it was taken under (radio, gain, reference, bin). Samples
// from another radio or another gain cannot judge this one, so the panel names what is missing.
test("samples only from other set-ups read NO BASELINE for this set-up, never a verdict", () => {
  const v = panel.provenanceVerdict(
    { ref_db: -31.8, soc_temp_c: 47 },
    { has_baseline: false, n_set_aside: 3 }
  );
  assert.match(v.text, /NO BASELINE/);
  assert.match(v.text, /this set-up/);
  assert.doesNotMatch(v.text, /REFERENCE HOLDS|REFERENCE DROPPED/);
  assert.equal(v.kind, "na");
});

test("a missing reference still comes first when other set-ups hold a baseline", () => {
  const v = panel.provenanceVerdict(
    { ref_db: null, soc_temp_c: 47 },
    { has_baseline: false, n_set_aside: 2 }
  );
  assert.match(v.text, /NO REFERENCE/);
  assert.equal(v.kind, "na");
});

test("with no sample recorded at all the panel still asks for a capture", () => {
  const v = panel.provenanceVerdict(
    { ref_db: -31.8, soc_temp_c: 47 },
    { has_baseline: false, n_set_aside: 0 }
  );
  assert.match(v.text, /NO BASELINE — capture one/);
  assert.equal(v.kind, "na");
});
