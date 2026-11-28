// ABOUTME: Tests the lesson page's tuning restore (lesson-tuning.js): one POST on load that never
// ABOUTME: blocks the page, panels whose first poll waits for it (or its timeout), and an honest note.

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");

const source = name => readFileSync(require.resolve(`../sigint101/static/${name}.js`), "utf8");

const RACK_HZ = 433_920_000;   // where the rack left the receivers
const HOME_HZ = 315_000_000;   // where the lesson expects them
const TIMEOUT_MS = 3000;       // how long the panels wait for the restore before polling anyway
// The room the rail leaves the note beside "Locked until you predict" at 800 px: 113 px, about
// 14 characters of its 13 px type. Each line the script writes itself must fit there whole.
const RAIL_CHARS = 14;

// The panels whose receivers the restore retunes, each with the poll its first tick sends.
const PANELS = [
  { name: "decode", selector: "[data-decode-panel]", poll: "/api/decode" },
  { name: "aprs", selector: "[data-aprs-panel]", poll: "/api/aprs" },
  { name: "pocsag", selector: "[data-pocsag-panel]", poll: "/api/pocsag" },
  { name: "acars", selector: "[data-acars-panel]", poll: "/api/acars" },
  { name: "rds", selector: "[data-rds-panel]", poll: "/api/rds" },
  { name: "dmr", selector: "[data-dmr-panel]", poll: "/api/dmr" },
  { name: "waterfall", selector: ".waterfall-wrap", poll: "/api/spectrum" },
];

const settle = () => new Promise(resolve => setImmediate(resolve));
const TEXT = Symbol("a plain-text body");

// An element that answers every lookup the panels make at load with another such element.
function stub() {
  const el = {
    textContent: "", className: "", innerHTML: "", hidden: false, title: "", children: [],
    classList: { contains: () => false, toggle: () => false, add() {}, remove() {} },
    style: {},
    querySelector: () => stub(),
    querySelectorAll: () => [],
    closest: () => null,
    getAttribute: () => null,
    setAttribute() {},
    addEventListener() {},
    appendChild() {},
    getContext: () => ({ fillRect() {}, drawImage() {}, clearRect() {} }),
  };
  return el;
}

// A lesson page in a fresh realm: a fake clock, a console whose retune lands when /api/tuning is
// answered, and the scripts run in page order, each in its own tag. As in a browser, a script that
// throws is reported and the next tag still runs.
// `band` puts a lesson band on the waterfall's canvas, as the lesson page's data-tune-* do.
// `clock` stands in for action-clock.js: the stamp the page puts on the learner's action.
// `gate` is what GET /api/tuning answers: the restore's state and what it waits on.
// A gate state that never answers: a status GET the console holds.
const HOLD = Symbol("held status answer");

function lessonPage({ lessonId = "7", panels = ["decode"], restore = "held", band = null,
                      clock = null, navigation = null,
                      gate = { restoring: true, waiting_on: null } } = {}) {
  let now = 0;
  let seq = 0;
  const timers = [];
  const server = { hz: RACK_HZ };
  const requests = [];
  const errors = [];
  // The note's two lines, the verdict and why, as the lesson page's rail carries them.
  const head = { textContent: "" };
  const why = { textContent: "" };
  // The full reason, shown under the rail when it is too long for the rail's line; a tap on it
  // closes it, and a tap on the rail's note opens it again.
  const full = { hidden: true, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } };
  const fullText = { textContent: "" };
  const note = {
    hidden: true, title: "", listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    querySelector: sel => ({ "[data-lesson-tuning-head]": head,
                             "[data-lesson-tuning-why]": why })[sel] || null,
    get lines() { return [head.textContent, why.textContent]; },
  };
  let answer = null;

  const fetch = (url, opts) => {
    const method = (opts && opts.method) || "GET";
    requests.push({ url, method, hz: server.hz, at: now });
    if (url.startsWith("/api/tuning") && method === "GET") {
      const state = typeof gate === "function" ? gate() : gate;
      if (state === HOLD) return new Promise(() => {});   // an answer that never comes
      return Promise.resolve({ ok: true, status: 200, json: async () => state });
    }
    if (url.startsWith("/api/tuning")) {
      if (restore === "lost") return Promise.reject(new TypeError("Failed to fetch"));
      return new Promise(resolve => {
        answer = (status, body) => {
          if (status === 200) server.hz = HOME_HZ;
          // A body of TEXT stands for a plain-text answer (the render proxy's), which is not JSON.
          const json = body === TEXT ? async () => { throw new SyntaxError("not JSON"); }
                                     : async () => body;
          resolve({ ok: status >= 200 && status < 300, status, json });
        };
      });
    }
    // The ISM panel's replay reads (5a-2): this kit is not replaying and has no recordings, so
    // the panel goes on to its live poll.
    if (url === "/api/replay/ism") {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ state: "idle" }) });
    }
    if (url === "/api/replay/recordings") {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ recordings: [] }) });
    }
    return new Promise(() => {});   // a poll the test never needs answered
  };

  const wanted = new Set(panels.map(name => PANELS.find(p => p.name === name).selector));
  const context = {
    document: {
      currentScript: null,
      querySelector: sel => ({ "[data-lesson-tuning]": note, "[data-lesson-tuning-full]": full,
                               "[data-lesson-tuning-full-text]": fullText })[sel] || null,
      querySelectorAll: sel => {
        if (!wanted.has(sel)) return [];
        if (sel === ".waterfall-wrap") {
          const canvas = stub();
          if (band) canvas.getAttribute = key => ({ "data-tune-low": band.low,
                                                    "data-tune-high": band.high })[key] || null;
          return [{ querySelector: key => (key === "canvas.waterfall" ? canvas : stub()),
                    closest: () => null }];
        }
        return [stub()];
      },
      createElement: () => stub(),
    },
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    fetch,
    setTimeout: (fn, ms) => { const id = ++seq; timers.push({ id, at: now + (ms || 0), fn }); return id; },
    clearTimeout: id => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
    console,
    listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
  };
  context.window = context;
  if (clock) {
    const stamp = () => (typeof clock === "function" ? clock() : clock);
    // The opening restore is stamped at the navigation start (Codex D-139 F2); a test that gives
    // a `navigation` stamp sees it there.
    context.ActionClock = { stamp, navigationStamp: () => (navigation || stamp()) };
  }

  const run = (name, attrs = {}) => {
    context.document.currentScript = { getAttribute: key => (key in attrs ? attrs[key] : null) };
    try {
      runInNewContext(source(name), context);
    } catch (err) {
      errors.push(`${name}: ${err.message}`);
    }
    context.document.currentScript = null;
  };

  run("lesson-tuning", { "data-lesson-id": lessonId });
  run("decoder-panel");
  panels.forEach(name => run(name));

  return {
    note, full, fullText, requests, errors, server,
    // The browser shows the page again from its back/forward cache.
    pageshow: async persisted => { context.listeners.pageshow({ persisted }); await settle(); },
    polls: poll => requests.filter(r => r.url.startsWith(poll)),
    answer: async (status, body = {}) => { answer(status, body); await settle(); },
    // Move the fake clock on, firing every timer that falls due, in order.
    advance: async ms => {
      const until = now + ms;
      for (;;) {
        timers.sort((a, b) => a.at - b.at || a.id - b.id);
        const next = timers[0];
        if (!next || next.at > until) break;
        timers.shift();
        now = next.at;
        next.fn();
        await settle();
      }
      now = until;
      await settle();
    },
  };
}

test("a slow restore never blocks the page: the script returns at once and taps keep running", async () => {
  // A real realm with real timers. The console takes 200 ms to answer /api/tuning. A synchronous
  // request is modelled as a browser runs one: the page's only thread waits inside send() until the
  // console answers, and nothing else (a tap, a timer) runs until then.
  const SLOW_MS = 200;
  const events = [];
  class BlockingXHR {
    open(method, url, async) { this.async = async !== false; }
    send() {
      if (this.async) { setTimeout(() => events.push("restore answered"), SLOW_MS); return; }
      const until = Date.now() + SLOW_MS;
      while (Date.now() < until) { /* the page is frozen */ }
      events.push("restore answered");
    }
  }
  const context = {
    document: { currentScript: { getAttribute: key => (key === "data-lesson-id" ? "7" : null) },
                querySelector: () => null },
    XMLHttpRequest: BlockingXHR,
    fetch: () => new Promise(resolve => setTimeout(() => {
      events.push("restore answered");
      resolve({ ok: true, status: 200, json: async () => ({ tuned: true }) });
    }, SLOW_MS)),
    setTimeout, clearTimeout, console,
  };
  context.window = context;

  setTimeout(() => events.push("tap handled"), 0);   // the learner taps as the page loads
  runInNewContext(source("lesson-tuning"), context);
  events.push("script returned");
  await new Promise(resolve => setTimeout(resolve, SLOW_MS + 100));

  assert.deepEqual(events, ["script returned", "tap handled", "restore answered"],
                   "the page must keep handling taps while the console restores the tuning");
});

for (const panel of PANELS) {
  test(`the ${panel.name} panel's first poll waits for the restore and finds the lesson's frequency`,
       async () => {
    const page = lessonPage({ panels: [panel.name] });
    await page.advance(1000);
    assert.deepEqual(page.polls(panel.poll), [],
                     "a panel must not poll while its receiver may still be on the rack's frequency");
    await page.answer(200, { tuned: true, lesson_id: 7 });
    const [first] = page.polls(panel.poll);
    assert.ok(first, "the panel polls once the restore answers, without waiting out the timeout");
    assert.equal(first.hz, HOME_HZ);
    assert.equal(page.note.hidden, true, "a restore that worked leaves nothing to say");
  });
}

test("the page posts its tuning once, as an asynchronous POST naming the lesson", async () => {
  const page = lessonPage({ lessonId: "7", panels: ["decode", "aprs"] });
  const posts = page.requests.filter(r => r.url.startsWith("/api/tuning"));
  assert.deepEqual(posts.map(r => [r.method, r.url]), [["POST", "/api/tuning?lesson_id=7"]]);
  assert.deepEqual(page.errors, []);
});

test("a restore still running at the timeout: the panels poll anyway and the rail says so", async () => {
  const page = lessonPage({ panels: ["decode", "waterfall"] });
  await page.advance(TIMEOUT_MS - 1);
  assert.deepEqual(page.polls("/api/decode"), []);
  assert.equal(page.note.hidden, true);

  await page.advance(1);
  // The panels ask now (the console holds their claims until the restore is done), and the rail
  // says the radios are still being tuned, in words that fit it whole.
  assert.equal(page.polls("/api/decode").length, 1);
  assert.equal(page.polls("/api/spectrum").length, 1);
  assert.equal(page.note.hidden, false);
  const [verdict, reason] = page.note.lines;
  assert.match(verdict, /^tuning/i);
  assert.ok(verdict.length <= RAIL_CHARS, `"${verdict}" does not fit the rail`);
  assert.equal(reason, "");

  await page.answer(200, { tuned: true, lesson_id: 7 });
  assert.equal(page.note.hidden, true, "once the radios are on the lesson's frequencies, the note goes");
  assert.equal(page.polls("/api/decode").length, 1, "a late answer does not start a second poll loop");
});

test("a restore held up by a calibration says the radio is busy measuring, not tuning", async () => {
  // The EM's ruling: past the timeout the rail asks the console's read-only gate state, and a
  // restore waiting on a ppm calibration is shown as that, never as "Tuning…".
  const page = lessonPage({ panels: ["decode"], gate: { restoring: true, waiting_on: "calibration" } });
  await page.advance(TIMEOUT_MS);
  assert.equal(page.note.hidden, false);
  assert.deepEqual(page.note.lines, ["Radio busy", "measuring ppm"]);
  for (const line of page.note.lines) assert.ok(line.length <= RAIL_CHARS, line);
  assert.deepEqual(page.requests.filter(r => r.method === "GET" && r.url === "/api/tuning")
                     .map(r => r.url), ["/api/tuning"]);
  await page.answer(200, { tuned: true, lesson_id: 7 });
  assert.equal(page.note.hidden, true);
});

test("the rail keeps the waiting reason current until the restore answers", async () => {
  // Codex D-139 F4: the rail read the gate's state once, so it still said "measuring ppm" after
  // the calibration had ended and the restore was tuning. It now asks again while the restore is
  // pending, and stops once it answers.
  let state = { restoring: true, waiting_on: "calibration" };
  const page = lessonPage({ panels: ["decode"], gate: () => state });
  await page.advance(TIMEOUT_MS);
  assert.deepEqual(page.note.lines, ["Radio busy", "measuring ppm"]);
  state = { restoring: true, waiting_on: null };          // the calibration ends
  await page.advance(1000);
  assert.deepEqual(page.note.lines, ["Tuning…", ""]);
  await page.answer(200, { tuned: true, lesson_id: 7 });
  assert.equal(page.note.hidden, true);
  const asked = page.requests.filter(r => r.method === "GET").length;
  await page.advance(5000);
  assert.equal(page.requests.filter(r => r.method === "GET").length, asked,
               "the rail kept asking after the restore answered");
});

test("the opening restore is stamped at the navigation start, a bfcache show stamped afresh", async () => {
  // Codex D-139 F2.
  let at = 3000;
  const page = lessonPage({ panels: ["decode"], navigation: { epoch: "5eed", at: 994 },
                            clock: () => ({ epoch: "5eed", at }) });
  await page.answer(200, { tuned: true, lesson_id: 7 });
  at = 9000;
  await page.pageshow(true);
  assert.deepEqual(page.requests.filter(r => r.method === "POST").map(r => r.url),
                   ["/api/tuning?lesson_id=7&epoch=5eed&at=994",
                    "/api/tuning?lesson_id=7&epoch=5eed&at=9000"]);
});

test("a restore still tuning past the timeout says so once the console confirms it", async () => {
  const page = lessonPage({ panels: ["decode"], gate: { restoring: true, waiting_on: null } });
  await page.advance(TIMEOUT_MS);
  assert.deepEqual(page.note.lines, ["Tuning…", ""]);
});

test("a refused restore says the radios were not retuned, with the reason, and the panels poll", async () => {
  const page = lessonPage({ panels: ["decode"] });
  await page.answer(503, { detail: "rtl_433 did not stop" });
  assert.equal(page.note.hidden, false);
  // The verdict and the reason each get a line of their own, so neither is cut to make room.
  assert.deepEqual(page.note.lines, ["Not retuned", "rtl_433 did not stop"]);
  const [first] = page.polls("/api/decode");
  assert.ok(first, "a refusal opens the panels at once");
  assert.equal(first.hz, RACK_HZ, "the panel reads where the receiver really is");
});

test("a lost console says the radios were not retuned, and the panels still poll", async () => {
  const page = lessonPage({ panels: ["decode"], restore: "lost" });
  await settle();
  assert.equal(page.note.hidden, false);
  assert.deepEqual(page.note.lines, ["Not retuned", "no console"]);
  assert.equal(page.polls("/api/decode").length, 1);
});

test("a refusal without a reason of its own still fits the rail, verdict and status", async () => {
  const page = lessonPage({ panels: ["decode"] });
  await page.answer(500, null);
  assert.deepEqual(page.note.lines, ["Not retuned", "error 500"]);
  for (const line of page.note.lines) {
    assert.ok(line.length <= RAIL_CHARS, `"${line}" does not fit the rail`);
  }
});

test("on a lesson page the restore sets the band, so the waterfall posts no band tune of its own",
     async () => {
  // A band tune of the page's own could be refused behind an earlier lesson's stuck restore and
  // leave the sweep off the band in silence; the restore sets it in order, under the same lock.
  const page = lessonPage({ panels: ["waterfall"], band: { low: "432920000", high: "434920000" } });
  await page.answer(200, { tuned: true, lesson_id: 7 });
  assert.deepEqual(page.requests.filter(r => r.url === "/api/tune"), []);
  assert.equal(page.polls("/api/spectrum").length, 1);
});

test("a waterfall with a band and no lesson restore still tunes to the band itself", async () => {
  const page = lessonPage({ lessonId: null, panels: ["waterfall"],
                            band: { low: "432920000", high: "434920000" } });
  await settle();
  const tunes = page.requests.filter(r => r.url === "/api/tune");
  assert.deepEqual(tunes.map(r => r.method), ["POST"]);
});

test("the restore carries the learner's action stamp, so the console orders it by that", () => {
  const page = lessonPage({ clock: { epoch: "5eed", at: 1234.5 } });
  const posts = page.requests.filter(r => r.url.startsWith("/api/tuning"));
  assert.deepEqual(posts.map(r => r.url), ["/api/tuning?lesson_id=7&epoch=5eed&at=1234.5"]);
});

test("a band the console refuses says so with the validator's own reason", async () => {
  const page = lessonPage({ panels: ["decode"] });
  const reason = "the span is narrower than one 125 kHz bin";
  await page.answer(422, { tuned: false, lesson_id: 7, reason });
  assert.equal(page.note.hidden, false);
  assert.deepEqual(page.note.lines, ["Not retuned", reason]);
});

test("a reason too long for the rail is shown whole under it, closed and reopened by a tap", async () => {
  // Codex D-132 F6: the rail gave the validator's reason 195 of the 410 px it needs, and the
  // ellipsis hid its useful end. A long reason opens whole under the rail, where it wraps.
  const page = lessonPage({ panels: ["decode"] });
  const reason = "span 100000 Hz is narrower than one 125000 Hz bin";
  await page.answer(422, { tuned: false, lesson_id: 7, reason });
  assert.equal(page.full.hidden, false);
  assert.equal(page.fullText.textContent, reason);
  page.full.listeners.click();
  assert.equal(page.full.hidden, true, "a tap on the reason closes it");
  assert.equal(page.note.hidden, false, "the rail still says it was not retuned");
  page.note.listeners.click();
  assert.equal(page.full.hidden, false, "a tap on the rail's note opens it again");
});

test("a reason that fits the rail opens nothing under it", async () => {
  for (const [status, body] of [[503, { reason: "no console" }], [405, TEXT],
                                [200, { tuned: false, skipped: { sweeper: "you retuned" } }]]) {
    const page = lessonPage({ panels: ["decode"] });
    await page.answer(status, body);
    assert.equal(page.note.hidden, false);
    assert.equal(page.full.hidden, true, `${status} opened the reason under the rail`);
  }
});

test("a restore that skipped a receiver for the learner's own tune says so, not done", async () => {
  const page = lessonPage({ panels: ["decode"] });
  await page.answer(200, { tuned: false, lesson_id: 7, skipped: { sweeper: "you retuned" } });
  assert.equal(page.note.hidden, false);
  assert.deepEqual(page.note.lines, ["Tuning skipped", "you retuned"]);
  for (const line of page.note.lines) assert.ok(line.length <= RAIL_CHARS, line);
});

test("a restore overtaken by a newer lesson says the lesson changed", async () => {
  const page = lessonPage({ panels: ["decode"] });
  await page.answer(200, { tuned: false, lesson_id: 7, skipped: { decoder: "a newer lesson" } });
  assert.deepEqual(page.note.lines, ["Tuning skipped", "lesson changed"]);
});

test("a restore refused as stale says the lesson changed", async () => {
  const page = lessonPage({ panels: ["decode"] });
  await page.answer(409, { tuned: false, lesson_id: 7, stale: true,
                           reason: "a newer lesson was opened" });
  assert.deepEqual(page.note.lines, ["Not retuned", "lesson changed"]);
});

test("under the render proxy the refused restore says so and the panels poll at once", async () => {
  // tools/render/render_proxy.py answers every POST with a plain-text 405, so on a render check
  // the restore never runs. The rail must say so, and no panel may wait out the timeout for it.
  const page = lessonPage({ panels: ["decode", "waterfall"] });
  await page.answer(405, TEXT);
  assert.equal(page.note.hidden, false);
  assert.deepEqual(page.note.lines, ["Not retuned", "error 405"]);
  assert.equal(page.polls("/api/decode").length, 1);
  assert.equal(page.polls("/api/spectrum").length, 1);
});

test("a page shown again from the back/forward cache posts its restore again, stamped now", async () => {
  // Review round 5: Back restored lesson A from the bfcache with no restore at all, so the
  // receivers stayed where lesson B left them. The page's clock still maps the browser's timeline
  // onto the console's (both ran on while it was frozen), so a stamp taken now is current, and
  // the restore is not stale against B's.
  let at = 1000;
  const page = lessonPage({ panels: ["decode"], clock: () => ({ epoch: "5eed", at }) });
  await page.answer(200, { tuned: true, lesson_id: 7 });
  at = 9000;                                   // the learner went to B and came Back
  await page.pageshow(false);                  // an ordinary load posts nothing more
  assert.equal(page.requests.filter(r => r.method === "POST").length, 1);
  await page.pageshow(true);
  const posts = page.requests.filter(r => r.method === "POST").map(r => r.url);
  assert.deepEqual(posts, ["/api/tuning?lesson_id=7&epoch=5eed&at=1000",
                           "/api/tuning?lesson_id=7&epoch=5eed&at=9000"]);
  await page.answer(409, { tuned: false, stale: true, reason: "a newer lesson was opened" });
  assert.deepEqual(page.note.lines, ["Not retuned", "lesson changed"],
                   "the second restore's answer is shown");
});

test("a page that names no lesson posts nothing and its panels poll at once", async () => {
  const page = lessonPage({ lessonId: null, panels: ["decode", "waterfall"] });
  await settle();
  assert.deepEqual(page.requests.filter(r => r.url.startsWith("/api/tuning")), []);
  assert.equal(page.polls("/api/decode").length, 1);
  assert.equal(page.polls("/api/spectrum").length, 1);
});

// --- round 7 (Codex D-146 F2): the rail names only an activity the console has just confirmed --

test("a held status answer stops naming the calibration, and the rail asks again", async () => {
  // Codex D-146 F2, probe 1: one status GET was held after the calibration ended. The rail sent no
  // further request and still said "measuring ppm". Each status request now has a time limit; an
  // unconfirmed status clears the activity, and the rail asks again.
  let state = { restoring: true, waiting_on: "calibration" };
  const page = lessonPage({ panels: ["decode"], gate: () => state });
  await page.advance(TIMEOUT_MS);
  assert.deepEqual(page.note.lines, ["Radio busy", "measuring ppm"]);
  state = HOLD;                                   // the next answer never comes
  await page.advance(1000 + 2000);
  assert.notDeepEqual(page.note.lines, ["Radio busy", "measuring ppm"],
                      "the rail still named a calibration it could no longer confirm");
  assert.ok(!/^tuning/i.test(page.note.lines[0]), "the rail named tuning it had not confirmed");
  assert.equal(page.note.hidden, false, "the rail went quiet while the restore was pending");
  const asked = page.requests.filter(r => r.method === "GET").length;
  state = { restoring: true, waiting_on: null };  // the console answers again: it is tuning
  await page.advance(2000);
  assert.ok(page.requests.filter(r => r.method === "GET").length > asked,
            "the rail stopped asking after one held answer");
  assert.deepEqual(page.note.lines, ["Tuning…", ""]);
});

test("an idle gate with the restore's answer still in flight shows no activity", async () => {
  // Codex D-146 F2, probe 2: the console had finished a refused restore and reported idle while the
  // POST's answer was still on its way, and the rail kept saying "Tuning…".
  const page = lessonPage({ panels: ["decode"], gate: { restoring: false, waiting_on: null } });
  await page.advance(TIMEOUT_MS);
  assert.equal(page.note.hidden, false);
  assert.ok(!/^tuning/i.test(page.note.lines[0]), `the rail said "${page.note.lines[0]}"`);
  assert.notDeepEqual(page.note.lines, ["Radio busy", "measuring ppm"]);
  for (const line of page.note.lines) assert.ok(line.length <= RAIL_CHARS, line);
  await page.advance(2000);
  assert.ok(!/^tuning/i.test(page.note.lines[0]), "a later poll of the idle gate said tuning");
  await page.answer(503, { reason: "rtl_433 did not stop" });
  assert.deepEqual(page.note.lines.slice(0, 1), ["Not retuned"]);
});

test("before the console has confirmed anything, the rail names no activity", async () => {
  const page = lessonPage({ panels: ["decode"], gate: () => HOLD });
  await page.advance(TIMEOUT_MS);
  assert.equal(page.note.hidden, false);
  assert.ok(!/^tuning/i.test(page.note.lines[0]), `the rail said "${page.note.lines[0]}"`);
  assert.notDeepEqual(page.note.lines, ["Radio busy", "measuring ppm"]);
});

test("the ISM panel's replay status read does not wait for the lesson's tuning", async () => {
  // Merging 5a-2 (replay): only the first live poll waits for the restore, because it claims the
  // radio. The replay status read is read-only, so a replay under way shows at once.
  const page = lessonPage({ panels: ["decode"] });
  assert.equal(page.polls("/api/replay/ism").length, 1, "the replay read waited for the tuning");
  assert.equal(page.polls("/api/decode").length, 0, "the live poll did not wait for the tuning");
  await page.answer(200, { tuned: true, lesson_id: 7 });
  assert.equal(page.polls("/api/decode").length, 1);
});

test("a faded lesson's tuning answer leaves the rail quiet and opens panels at once", async () => {
  const page = lessonPage({ lessonId: "26", panels: ["decode"] });
  await page.answer(200, { tuned: false, lesson_id: 26, faded: true });
  assert.equal(page.note.hidden, true, "faded no-op restore must not show a Tuning skipped note");
  assert.equal(page.full.hidden, true);
  assert.equal(page.polls("/api/decode").length, 1);
});

test("a faded lesson refused while an earlier restore is still in progress shows the note", async () => {
  const page = lessonPage({ lessonId: "26", panels: ["decode"] });
  const reason = "an earlier restore is still in progress";
  await page.answer(503, { tuned: false, lesson_id: 26, faded: true, busy: true, reason });
  assert.equal(page.note.hidden, false);
  assert.deepEqual(page.note.lines, ["Not retuned", reason]);
  assert.equal(page.full.hidden, false);
  assert.equal(page.fullText.textContent, reason);
  assert.equal(page.polls("/api/decode").length, 1);
});
