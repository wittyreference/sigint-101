// ABOUTME: Tests the Wi-Fi survey panel's pure helpers and strict envelope-only rendering.
// ABOUTME: Uses node's built-in runner; verifies no field outside the envelope list ever renders.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/survey.js");

test("envelope allowlist excludes any payload field by construction", () => {
  assert.deepStrictEqual(panel.ENVELOPE_FIELDS, ["ssid", "bssid", "channel", "signal", "security"]);
  assert.deepStrictEqual(panel.CENSUS_FIELDS, ["channel", "count", "hidden", "open", "strongest", "ssids"]);
  for (const forbidden of ["payload", "data", "body", "content", "packet", "frame"]) {
    assert.ok(!panel.ENVELOPE_FIELDS.includes(forbidden));
    assert.ok(!panel.CENSUS_FIELDS.includes(forbidden));
  }
});

test("channelLabel, countLabel and signalLabel format census metrics cleanly", () => {
  assert.equal(panel.channelLabel({ channel: 6 }), "CH 6");
  assert.equal(panel.channelLabel({}), "CH —");
  assert.equal(panel.countLabel({ count: 1 }), "1 AP");
  assert.equal(panel.countLabel({ count: 3 }), "3 APs");
  assert.equal(panel.countLabel({ count: 0 }), "0 APs");
  assert.equal(panel.signalLabel({ strongest: 84 }), "84%");
  assert.equal(panel.signalLabel({}), "—");
});

test("summary formats SSIDs, hidden count, and open count from metadata alone", () => {
  assert.equal(
    panel.summary({ ssids: ["CafeNet", "Lab:5G"], hidden: 1, open: 1 }),
    "CafeNet, Lab:5G · 1 hidden · 1 open",
  );
  assert.equal(panel.summary({ ssids: [], hidden: 2, open: 0 }), "2 hidden");
  assert.equal(panel.summary({ ssids: ["Guest"], hidden: 0, open: 1 }), "Guest · 1 open");
  assert.equal(panel.summary({ ssids: ["A", "B", "C", "D"], hidden: 0, open: 0 }), "A, B, +2");
  assert.equal(panel.summary({}), "(no SSID)");
});

test("envelopeRow drops any field outside the envelope allowlist", () => {
  const dirty = {
    channel: 6,
    count: 2,
    hidden: 1,
    open: 1,
    strongest: 70,
    ssids: ["GuestOpen"],
    payload: "SECRET_EMAIL_BODY",
    data: "USER_PASSWORD_123",
    body: "PRIVATE_HTML",
    content: "DO_NOT_RENDER",
  };
  const clean = panel.envelopeRow(dirty);
  assert.deepStrictEqual(Object.keys(clean).sort(), [...panel.CENSUS_FIELDS].sort());
  const rendered = [
    panel.channelLabel(clean),
    panel.countLabel(clean),
    panel.summary(clean),
    panel.signalLabel(clean),
  ].join(" ");
  assert.ok(!rendered.includes("SECRET_EMAIL_BODY"));
  assert.ok(!rendered.includes("USER_PASSWORD_123"));
  assert.ok(!rendered.includes("PRIVATE_HTML"));
  assert.ok(!rendered.includes("DO_NOT_RENDER"));
});

test("pollAnswer preserves honest no-radio refusals and rejects malformed replies", () => {
  const down = panel.pollAnswer(503, {
    available: false,
    cause: "no_radio",
    reason: "no Wi-Fi radio on this host — SIGINT 101 will not fake a scan",
    channels: [],
  });
  assert.equal(down.available, false);
  assert.equal(down.cause, "no_radio");

  const ok = panel.pollAnswer(200, {
    available: true,
    command: "nmcli -t -f SSID,BSSID,CHAN,SIGNAL,SECURITY dev wifi list",
    channels: [{ channel: 1, count: 1, hidden: 0, open: 0, strongest: 80, ssids: ["CafeNet"] }],
  });
  assert.equal(ok.available, true);
  assert.equal(ok.channels.length, 1);

  const bad200 = panel.pollAnswer(200, { available: true });
  assert.equal(bad200.available, false);
  assert.match(bad200.reason, /HTTP 200/);

  const offline = panel.pollAnswer(null, null);
  assert.equal(offline.available, false);
  assert.match(offline.reason, /Lost contact/);
});
