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
function lessonPage({ lessonId = "7", panels = ["decode"], restore = "held" } = {}) {
  let now = 0;
  let seq = 0;
  const timers = [];
  const server = { hz: RACK_HZ };
  const requests = [];
  const errors = [];
  const note = { hidden: true, textContent: "", title: "" };
  let answer = null;

  const fetch = (url, opts) => {
    const method = (opts && opts.method) || "GET";
    requests.push({ url, method, hz: server.hz, at: now });
    if (url.startsWith("/api/tuning")) {
      if (restore === "lost") return Promise.reject(new TypeError("Failed to fetch"));
      return new Promise(resolve => {
        answer = (status, body) => {
          if (status === 200) server.hz = HOME_HZ;
          resolve({ ok: status >= 200 && status < 300, status, json: async () => body });
        };
      });
    }
    return new Promise(() => {});   // a poll the test never needs answered
  };

  const wanted = new Set(panels.map(name => PANELS.find(p => p.name === name).selector));
  const context = {
    document: {
      currentScript: null,
      querySelector: sel => (sel === "[data-lesson-tuning]" ? note : null),
      querySelectorAll: sel => {
        if (!wanted.has(sel)) return [];
        if (sel === ".waterfall-wrap") {
          const canvas = stub();
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
  };
  context.window = context;

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
    note, requests, errors, server,
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
                querySelector: () => ({ hidden: true, textContent: "", title: "" }) },
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
  // Polling now reads wherever the receiver really is, and the rail says why it may not be the
  // lesson's frequency yet.
  assert.equal(page.polls("/api/decode").length, 1);
  assert.equal(page.polls("/api/spectrum").length, 1);
  assert.equal(page.note.hidden, false);
  assert.match(page.note.textContent, /tuning/i);

  await page.answer(200, { tuned: true, lesson_id: 7 });
  assert.equal(page.note.hidden, true, "once the radios are on the lesson's frequencies, the note goes");
  assert.equal(page.polls("/api/decode").length, 1, "a late answer does not start a second poll loop");
});

test("a refused restore says the radios were not retuned, with the reason, and the panels poll", async () => {
  const page = lessonPage({ panels: ["decode"] });
  await page.answer(503, { detail: "rtl_433 did not stop" });
  assert.equal(page.note.hidden, false);
  assert.match(page.note.textContent, /not retuned/i);
  assert.match(page.note.textContent, /rtl_433 did not stop/);
  const [first] = page.polls("/api/decode");
  assert.ok(first, "a refusal opens the panels at once");
  assert.equal(first.hz, RACK_HZ, "the panel reads where the receiver really is");
});

test("a lost console says the radios were not retuned, and the panels still poll", async () => {
  const page = lessonPage({ panels: ["decode"], restore: "lost" });
  await settle();
  assert.equal(page.note.hidden, false);
  assert.match(page.note.textContent, /not retuned/i);
  assert.match(page.note.textContent, /lost contact/i);
  assert.equal(page.polls("/api/decode").length, 1);
});

test("a page that names no lesson posts nothing and its panels poll at once", async () => {
  const page = lessonPage({ lessonId: null, panels: ["decode", "waterfall"] });
  await settle();
  assert.deepEqual(page.requests.filter(r => r.url.startsWith("/api/tuning")), []);
  assert.equal(page.polls("/api/decode").length, 1);
  assert.equal(page.polls("/api/spectrum").length, 1);
});
