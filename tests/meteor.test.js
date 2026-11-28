// ABOUTME: Tests the Meteor LRPT rack panel helpers (sigint101/static/meteor.js).
// ABOUTME: Covers search/lock phases at both channels, past-picture retention, captions, and non-stopped states.

const test = require("node:test");
const assert = require("node:assert");
const m = require("../sigint101/static/meteor.js");

test("test_meteor_panel_searches_until_both_blocks_sync", () => {
  const starting = m.phaseView({
    state: "starting", freq_hz: 137900000, mode: "72k", lock: "unknown",
  });
  assert.equal(starting.badge, "STARTING");
  assert.match(starting.phase, /137\.900 MHz/);

  const searching79 = m.phaseView({
    state: "searching", freq_hz: 137900000, mode: "72k", lock: "searching",
    reason: "Listening on 137.900 MHz · searching",
  });
  assert.equal(searching79.badge, "LIVE");
  assert.equal(searching79.phase, "Listening on 137.900 MHz · searching");

  const searching71 = m.phaseView({
    state: "searching", freq_hz: 137100000, mode: "80k", lock: "unknown",
    reason: "Listening on 137.100 MHz · searching",
  });
  assert.equal(searching71.badge, "LIVE");
  assert.equal(searching71.phase, "Listening on 137.100 MHz · searching");

  const locked71 = m.phaseView({
    state: "receiving", freq_hz: 137100000, mode: "80k", lock: "locked",
  });
  assert.equal(locked71.badge, "LIVE");
  assert.equal(locked71.phase, "Locked on 137.100 MHz · decoding frames");

  const decoding = m.phaseView({
    state: "decoding", freq_hz: 137900000, mode: "72k", elapsed_s: 74.2,
  });
  assert.equal(decoding.badge, "LIVE");
  assert.match(decoding.phase, /Decoding pictures/);
  assert.match(decoding.phase, /1:14/);
});

test("test_meteor_panel_keeps_past_pictures_on_refusal_and_lost_contact", () => {
  const saved = [{
    id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    started_at: 1732795200,
    ended_at: 1732795500,
    freq_hz: 137900000,
    mode: "72k",
    command: "satdump live meteor_m2-x_lrpt /tmp/x --finish_processing",
    radio: { kind: "rtl-sdr", serial: "rx", index: 0 },
    satellite: "METEOR-M2 3",
    result: "done",
    lock_ever: true,
    ended_by: "timeout",
    images: [{ path: "meteor/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/MSU-MR-1.png", product: "1" }],
  }];

  // POST refusal with missing passes preserves last rendered gallery.
  assert.deepEqual(m.resolvePasses({ available: false, reason: "no RTL-SDR" }, saved), saved);
  // Lost contact (null body) preserves last rendered gallery.
  assert.deepEqual(m.resolvePasses(null, saved), saved);
  // Later no_picture with empty passes preserves last rendered gallery.
  assert.deepEqual(m.resolvePasses({ state: "no_picture", passes: [] }, saved), saved);
  // Non-empty fresh passes replace the list.
  const newer = [{ ...saved[0], id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" }];
  assert.deepEqual(m.resolvePasses({ state: "done", passes: newer }, saved), newer);
});

test("test_meteor_panel_renders_caption_and_launch_details", () => {
  const passRow = {
    id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    started_at: 1732795200,
    ended_at: 1732795500,
    freq_hz: 137100000,
    mode: "80k",
    command: "satdump live meteor_m2-x_lrpt_80k /tmp/x --frequency 137100000 --finish_processing",
    radio: { kind: "rtl-sdr", serial: "rx42", index: 2 },
    satellite: "Meteor (unidentified)",
    result: "done",
    lock_ever: true,
    ended_by: "handoff",
    images: [{ path: "meteor/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/MSU-MR-rgb.png", product: "rgb" }],
  };
  const cap = m.passCaption(passRow, passRow.images[0]);
  assert.match(cap, /Meteor \(unidentified\)/);
  assert.match(cap, /137\.100 MHz/);
  assert.match(cap, /rgb/);
  assert.match(cap, /UTC/);
  assert.match(cap, /cut short/i);

  const det = m.passDetails(passRow);
  assert.match(det.radio, /RTL-SDR rx42/);
  assert.equal(det.command, passRow.command);
});

test("test_meteor_panel_idle_and_interrupted_are_not_stopped", () => {
  for (const body of [
    { state: "idle", reason: "Ready to listen", freq_hz: 137900000 },
    { state: "interrupted", reason: "SatDump exceeded its finishing deadline.", ended_by: "watchdog" },
    { state: "interrupted", reason: "Console shut down.", ended_by: "shutdown" },
    { state: "interrupted", reason: "SatDump stderr did not reach EOF." },
  ]) {
    const view = m.phaseView(body);
    assert.notEqual(view.badge, "STOPPED");
    assert.doesNotMatch(view.phase + " " + view.note, /retrying now/i);
  }
  const idle = m.phaseView({ state: "idle", reason: "Ready to listen" });
  assert.equal(idle.phase, "Ready to listen");

  const stopped = m.phaseView({
    state: "stopped", reason: "SatDump stopped before releasing the radio (exit 1)", retry_in: 8,
  });
  assert.equal(stopped.badge, "STOPPED");
  assert.match(stopped.note, /retrying in 8 s/i);
});
