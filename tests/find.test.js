// ABOUTME: Tests sigint101/static/find.js pure helpers (marks, countdown clock) and polling race
// ABOUTME: isolation so a pre-Start poll answer never overwrites the Start response.

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");

const find = require("../sigint101/static/find.js");

test("markFor gives each of the four states a distinct word and class", () => {
  const states = ["idle", "active", "passed", "expired"];
  const marks = states.map((s) => find.markFor(s));
  const words = new Set(marks.map((m) => m.word));
  const classes = new Set(marks.map((m) => m.cls));

  assert.equal(words.size, 4);
  assert.equal(classes.size, 4);
  assert.equal(find.markFor("idle").word, "READY");
  assert.equal(find.markFor("active").word, "HUNTING");
  assert.equal(find.markFor("passed").word, "FOUND");
  assert.equal(find.markFor("expired").word, "TIME UP");
});

test("clock formats mm:ss and clamps negative or non-finite values to 00:00", () => {
  assert.equal(find.clock(900), "15:00");
  assert.equal(find.clock(65), "01:05");
  assert.equal(find.clock(59.9), "00:59");
  assert.equal(find.clock(0), "00:00");
  assert.equal(find.clock(-12), "00:00");
  assert.equal(find.clock(NaN), "00:00");
  assert.equal(find.clock(null), "00:00");
});

test("a poll answer sent before Start is dropped once Start has fired", async () => {
  const markEl = { textContent: "READY", className: "find-mark find-mark--idle" };
  const clockEl = { textContent: "00:00", hidden: false };
  const startBtn = {
    textContent: "Start",
    listeners: {},
    addEventListener(type, fn) {
      this.listeners[type] = fn;
    },
  };
  const evidenceEl = { textContent: "" };
  const screenEl = {
    dataset: { unitId: "4" },
    getAttribute(name) {
      return name === "data-unit-id" ? "4" : null;
    },
    querySelector(sel) {
      return {
        "[data-find-mark]": markEl,
        "[data-find-clock]": clockEl,
        "[data-find-start]": startBtn,
        "[data-find-evidence]": evidenceEl,
      }[sel] || null;
    },
  };

  let resolveStalePoll;
  let resolveStartPost;
  const fetches = [];
  const timers = [];

  runInNewContext(readFileSync(require.resolve("../sigint101/static/find.js"), "utf8"), {
    document: {
      querySelector: (sel) => (sel === "[data-find-screen]" ? screenEl : null),
      querySelectorAll: (sel) => (sel === "[data-find-tile]" ? [] : []),
    },
    window: {},
    setTimeout: (fn) => {
      timers.push(fn);
      return timers.length;
    },
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
    fetch: (url, init) => {
      const method = (init && init.method) || "GET";
      fetches.push({ url, method });
      if (method === "GET") {
        return new Promise((resolve) => {
          resolveStalePoll = resolve;
        });
      }
      return new Promise((resolve) => {
        resolveStartPost = resolve;
      });
    },
  });

  const settle = () => new Promise((r) => setImmediate(r));

  // Initial GET poll is in flight.
  assert.equal(fetches.length, 1);
  assert.equal(fetches[0].method, "GET");

  // Learner taps Start before the initial GET poll resolves.
  startBtn.listeners.click();
  assert.equal(fetches.length, 2);
  assert.equal(fetches[1].method, "POST");
  assert.equal(fetches[1].url, "/api/challenges/4/start");

  // Start POST resolves first with HUNTING (active, 900 s).
  resolveStartPost({
    ok: true,
    status: 200,
    json: async () => ({
      unit_id: 4,
      state: "active",
      remaining_s: 900,
      window_s: 900,
      started_at: 1000,
      reason: null,
      latest_pass: null,
      passes: 0,
      error: null,
    }),
  });
  await settle();
  assert.equal(markEl.textContent, "HUNTING");
  assert.equal(clockEl.textContent, "15:00");
  assert.equal(startBtn.textContent, "Restart");

  // Now the stale pre-Start GET poll resolves with idle/READY — MUST be ignored!
  resolveStalePoll({
    ok: true,
    status: 200,
    json: async () => ({
      unit_id: 4,
      state: "idle",
      remaining_s: 0,
      window_s: 900,
      started_at: null,
      reason: null,
      latest_pass: null,
      passes: 0,
      error: null,
    }),
  });
  await settle();
  assert.equal(markEl.textContent, "HUNTING");
  assert.equal(clockEl.textContent, "15:00");
  assert.equal(startBtn.textContent, "Restart");
});

test("Start renders JSON error reason from a 500 response on the screen", async () => {
  const markEl = { textContent: "READY", className: "find-mark find-mark--idle" };
  const clockEl = { textContent: "00:00", hidden: false };
  const startBtn = {
    textContent: "Start",
    listeners: {},
    addEventListener(type, fn) {
      this.listeners[type] = fn;
    },
  };
  const evidenceEl = { textContent: "" };
  const screenEl = {
    getAttribute(name) {
      return name === "data-unit-id" ? "4" : null;
    },
    querySelector(sel) {
      return {
        "[data-find-mark]": markEl,
        "[data-find-clock]": clockEl,
        "[data-find-start]": startBtn,
        "[data-find-evidence]": evidenceEl,
      }[sel] || null;
    },
  };

  runInNewContext(readFileSync(require.resolve("../sigint101/static/find.js"), "utf8"), {
    document: {
      querySelector: (sel) => (sel === "[data-find-screen]" ? screenEl : null),
      querySelectorAll: () => [],
    },
    window: {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
    fetch: async (_url, init) => {
      if (init && init.method === "POST") {
        return {
          ok: false,
          status: 500,
          json: async () => ({
            unit_id: 4,
            state: "idle",
            remaining_s: 0,
            error: "the live find could not be started: database is locked",
          }),
        };
      }
      return new Promise(() => {});
    },
  });

  startBtn.listeners.click();
  await new Promise((r) => setImmediate(r));
  assert.equal(
    evidenceEl.textContent,
    "the live find could not be started: database is locked"
  );
});
