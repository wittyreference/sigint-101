// ABOUTME: Tests the Wi-Fi survey panel's pure helpers and strict envelope-only rendering.
// ABOUTME: Uses node's built-in runner; verifies band+channel keying and no payload field ever renders.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/survey.js");

test("envelope allowlist includes band/freq metadata and excludes any payload field", () => {
  assert.deepStrictEqual(
    panel.ENVELOPE_FIELDS,
    ["ssid", "bssid", "band", "channel", "freq_mhz", "signal", "security"],
  );
  assert.deepStrictEqual(
    panel.CENSUS_FIELDS,
    ["band", "channel", "count", "hidden", "open", "strongest", "ssids"],
  );
  for (const forbidden of ["payload", "data", "body", "content", "packet", "frame"]) {
    assert.ok(!panel.ENVELOPE_FIELDS.includes(forbidden));
    assert.ok(!panel.CENSUS_FIELDS.includes(forbidden));
  }
});

test("channelLabel distinguishes bands so 2.4 GHz and 6 GHz channel 1 stay separate", () => {
  assert.equal(panel.channelLabel({ band: "2.4 GHz", channel: 1 }), "2.4G CH 1");
  assert.equal(panel.channelLabel({ band: "5 GHz", channel: 36 }), "5G CH 36");
  assert.equal(panel.channelLabel({ band: "6 GHz", channel: 1 }), "6G CH 1");
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
    band: "2.4 GHz",
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

test("pollAnswer preserves honest radio-off refusals and rejects malformed replies", () => {
  const down = panel.pollAnswer(503, {
    available: false,
    cause: "no_radio",
    reason: "Wi-Fi radio is off — enable Wi-Fi to run the survey",
    channels: [],
  });
  assert.equal(down.available, false);
  assert.equal(down.cause, "no_radio");

  const ok = panel.pollAnswer(200, {
    available: true,
    command: "nmcli -t -f SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY dev wifi list --rescan no",
    channels: [
      { band: "2.4 GHz", channel: 1, count: 1, hidden: 0, open: 0, strongest: 80, ssids: ["CafeNet"] },
    ],
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

test("cached census badges CACHED with age and never claims LIVE or heard in this scan", () => {
  assert.equal(panel.formatAge(0), "0s");
  assert.equal(panel.formatAge(45), "45s");
  assert.equal(panel.formatAge(90), "1m");
  assert.equal(panel.formatAge(3600), "1h");
  assert.equal(panel.formatAge(null), "");
  assert.equal(panel.formatAge(-1), "");

  assert.deepStrictEqual(panel.badgeState({ cached: true, age_s: 45 }), {
    text: "CACHED · 45s",
    cls: "decoder__status decoder__status--cached",
  });
  assert.deepStrictEqual(panel.badgeState({ cached: true }), {
    text: "CACHED",
    cls: "decoder__status decoder__status--cached",
  });
  assert.equal(
    panel.emptyMessage({ cached: true, age_s: 12 }),
    "NetworkManager's cached list is empty",
  );
  assert.ok(!/heard in this scan/i.test(panel.emptyMessage({ cached: true, age_s: 0 })));
});
