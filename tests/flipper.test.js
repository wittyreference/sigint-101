// ABOUTME: Tests the Flipper panel: the identity line, the radio state words, what each answer shows.
// ABOUTME: Node's built-in runner; the poll runs in a stand-in page (flipper_poll.js) for each answer.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/flipper.js");

test("the device line is model, firmware and region, joined", () => {
  assert.equal(
    panel.deviceLine({ model: "Flipper Zero", firmware: "1.4.3", region: "US" }),
    "Flipper Zero · fw 1.4.3 · US");
});

test("a missing region is omitted, not shown blank", () => {
  assert.equal(
    panel.deviceLine({ model: "Flipper Zero", firmware: "1.4.3", region: "" }),
    "Flipper Zero · fw 1.4.3");
  assert.equal(panel.deviceLine(null), "");
});

test("the radio state is words, not a colour, and empty when unknown", () => {
  assert.equal(panel.radioLine({ radio_alive: true }), "radio: alive");
  assert.equal(panel.radioLine({ radio_alive: false }), "radio: down");
  assert.equal(panel.radioLine({}), "");
  assert.equal(panel.radioLine(null), "");
});

// C33: the panel's poll, run against one answer in a stand-in page (tests/flipper_poll.js). A
// server fault can answer 500 with plain text. The panel shows the HTTP status for an
// unusable reply, and the shared badge and explanation for a refusal from the route.
const { showPoll, macroText } = require("./flipper_poll.js");
const CHECKING = macroText("data-flipper-empty").text;
const RETRY_MS = 4000;
const POLL_MS = 2000;

function assertNoReading(shown, words) {
  assert.equal(shown.badge, "UNAVAILABLE", JSON.stringify(shown));
  assert.match(shown.badgeClass, /decoder__status--empty/);
  assert.notEqual(shown.empty, CHECKING);
  assert.ok(!shown.empty.includes("no Flipper on USB"), shown.empty);   // an error is no absence
  assert.ok(shown.empty.includes(words), shown.empty);
  assert.equal(shown.emptyHidden, false);
  assert.equal(shown.cardHidden, true);
  assert.deepEqual(shown.nextPollMs, [RETRY_MS]);                       // and it asks again
}

test("the stand-in page starts as the template does, checking", () => {
  assert.equal(CHECKING, "Checking receiver…");
});

test("a server fault's plain-text 500 shows no reading with its status, not reading forever", async () => {
  assertNoReading(await showPoll(500, "Internal Server Error"), "500");
});

test("a JSON error with no reason shows its status, not 'no Flipper on USB'", async () => {
  assertNoReading(await showPoll(500, JSON.stringify({ detail: "Internal Server Error" })), "500");
  assertNoReading(await showPoll(503, JSON.stringify({ available: false })), "503");
});

test("a page from something in between (a 502 HTML page) shows its status", async () => {
  assertNoReading(await showPoll(502, "<html><body>Bad Gateway</body></html>"), "502");
});

test("a good status with a body that is not JSON is no reading either", async () => {
  assertNoReading(await showPoll(200, "<html>a captive page</html>"), "200");
});

test("a good status with no device in it is no reading, never a device card", async () => {
  // Drawn as a reading, {} showed LIVE and a card named "Flipper" that no device reported.
  for (const body of [{}, [], { available: true }, { available: true, device: null },
                      { available: true, device: "Flipper" }, { device: {} }]) {
    assertNoReading(await showPoll(200, JSON.stringify(body)), "200");
  }
});

test("no answer at all shows that the console was not reached", async () => {
  const shown = await showPoll(null, "");
  const expected = "Lost contact with the console.";
  assert.strictEqual(shown.empty, expected);
  assertNoReading(shown, expected);
});

test("the route's refusals keep their exact reason or shared next-step message", async () => {
  const reason = "cannot open the Flipper: [Errno 13] Permission denied "
    + "(usb-Flipper_Devices_Inc._Flipper_Abcdefgh_flip_Abcdefgh-if00)";
  const shown = await showPoll(503, JSON.stringify(
    { available: false, cause: "unavailable", reason: reason, command: null, device: null }));
  assert.equal(shown.badge, "UNAVAILABLE");
  assert.equal(shown.empty, reason);
  assert.equal(shown.cardHidden, true);
  assert.deepEqual(shown.nextPollMs, [RETRY_MS]);

  const absent = await showPoll(503, JSON.stringify({ available: false, cause: "no_radio",
    reason: "no Flipper attached", message: "No Flipper on USB.",
    next_step: "Connect the Flipper over USB." }));
  assert.equal(absent.badge, "NO RADIO");
  assert.equal(absent.empty, "No Flipper on USB. · Connect the Flipper over USB.");
  assert.equal(absent.cardHidden, true);
  assert.deepEqual(absent.nextPollMs, [RETRY_MS]);
});

test("a device read shows the card and polls gently", async () => {
  const shown = await showPoll(200, JSON.stringify({ available: true, command: null, device: {
    model: "Flipper Zero", name: "Ex4mple", firmware: "1.4.3", region: "US", radio_alive: true } }));
  assert.equal(shown.badge, "LIVE");
  assert.equal(shown.name, "Ex4mple");
  assert.equal(shown.line, "Flipper Zero · fw 1.4.3 · US");
  assert.equal(shown.cardHidden, false);
  assert.equal(shown.emptyHidden, true);
  assert.deepEqual(shown.nextPollMs, [POLL_MS]);
});
