// ABOUTME: Tests the Flock panel's pure helpers — the tier label, the device ident, the summary.
// ABOUTME: Uses node's built-in runner; the polling and DOM wiring are deliberately not exercised.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/flock.js");

test("the tier label names the confidence, strongest at tier 4", () => {
  assert.equal(panel.tierLabel(4), "STRONG HIT");
  assert.equal(panel.tierLabel(3), "wildcard probe");
  assert.equal(panel.tierLabel(2), "OUI match");
  assert.equal(panel.tierLabel(1), "weak (addr)");
  assert.equal(panel.tierLabel(0), "SSID keyword");
  assert.equal(panel.tierLabel(undefined), "—");
});

test("the ident is the MAC, falling back to the OUI then a dash", () => {
  assert.equal(panel.ident({ mac: "b4:1e:52:00:00:01", oui: "b4:1e:52" }), "b4:1e:52:00:00:01");
  assert.equal(panel.ident({ oui: "b4:1e:52" }), "b4:1e:52");
  assert.equal(panel.ident({}), "—");
  assert.equal(panel.ident(null), "—");
});

test("the summary is the rule, with the hunted SSID quoted when present", () => {
  assert.equal(panel.summary({ rule: "probe-request IE fingerprint" }), "probe-request IE fingerprint");
  assert.equal(panel.summary({ rule: "SSID keyword", ssid: "Flock Safety" }),
    'SSID keyword · "Flock Safety"');
  assert.equal(panel.summary({}), "(no detail)");
  assert.equal(panel.summary(null), "(no detail)");
});
