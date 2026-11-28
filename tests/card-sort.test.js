// ABOUTME: Tests the card-sort panel: its pure helpers, and the panel itself on a stand-in DOM
// ABOUTME: (saving, the not-saved mark, score, resume). Runs under node's built-in test runner.

const test = require("node:test");
const assert = require("node:assert");
const panel = require("../sigint101/static/card-sort.js");

const CARDS = [
  { key: "c1", text: "Tune airband", answer: "legal", why: "Open aviation traffic.", cite: "18 USC 2511(2)(g)(ii)(IV)" },
  { key: "c2", text: "Read pager text", answer: "illegal", why: "Carrier page text is protected.", cite: "18 USC 2511(1)(a)" },
  { key: "c3", text: "Send Meshtastic", answer: "depends", why: "Needs Part 15 compliance.", cite: "47 CFR 15.247" },
];

test("SORT_CHOICES and choiceLabel name the three buckets cleanly", () => {
  assert.deepStrictEqual(panel.SORT_CHOICES, ["legal", "illegal", "depends"]);
  assert.equal(panel.choiceLabel("legal"), "Legal");
  assert.equal(panel.choiceLabel("illegal"), "Illegal");
  assert.equal(panel.choiceLabel("depends"), "Depends");
  assert.equal(panel.choiceLabel("unknown"), "—");
});

test("summarizeScore counts answered and correct cards across the deck", () => {
  assert.deepStrictEqual(panel.summarizeScore(CARDS, {}), { answered: 0, correct: 0, total: 3 });
  assert.deepStrictEqual(
    panel.summarizeScore(CARDS, {
      c1: { choice: "legal", correct: true },
      c2: { choice: "legal", correct: false },
    }),
    { answered: 2, correct: 1, total: 3 },
  );
  // Stale keys outside the deck are ignored.
  assert.deepStrictEqual(
    panel.summarizeScore(CARDS, { ghost: { choice: "legal", correct: true } }),
    { answered: 0, correct: 0, total: 3 },
  );
});

test("firstUnansweredIndex resumes on the first unanswered card or 0 when finished", () => {
  assert.equal(panel.firstUnansweredIndex(CARDS, {}), 0);
  assert.equal(panel.firstUnansweredIndex(CARDS, { c1: { choice: "legal", correct: true } }), 1);
  assert.equal(
    panel.firstUnansweredIndex(CARDS, {
      c1: { choice: "legal", correct: true },
      c2: { choice: "illegal", correct: true },
      c3: { choice: "depends", correct: true },
    }),
    0,
  );
});

test("stepCard stays within bounds and returns -1 past either end", () => {
  assert.equal(panel.stepCard(0, -1, 3), -1);
  assert.equal(panel.stepCard(0, 1, 3), 1);
  assert.equal(panel.stepCard(2, 1, 3), -1);
  assert.equal(panel.stepCard(2, -1, 3), 1);
});

test("summarizeScore scores each saved choice against the current answer key, not the stored flag", () => {
  // F8.2: the per-card tag recomputes from the key, so the tally must too, or a changed key in the
  // pack leaves the score and the tags disagreeing.
  assert.deepStrictEqual(
    panel.summarizeScore(CARDS, {
      c1: { choice: "illegal", correct: true },   // stored as right under an older key; wrong now
      c2: { choice: "illegal", correct: true },
    }),
    { answered: 2, correct: 1, total: 3 },
  );
  assert.deepStrictEqual(
    panel.summarizeScore(CARDS, { c3: { choice: "depends", correct: false } }),  // right now
    { answered: 1, correct: 1, total: 3 },
  );
});

test("saveAnswer posts with keepalive and reports a refusal or a lost request as not saved", async () => {
  const calls = [];
  const fetchOk = (url, opts) => { calls.push([url, opts]); return Promise.resolve({ ok: true }); };
  assert.equal(await panel.saveAnswer(fetchOk, "5", "c2", "illegal"), true);
  assert.equal(calls.length, 1);
  const [url, opts] = calls[0];
  assert.equal(url, "/api/sort?lesson_id=5");
  assert.equal(opts.method, "POST");
  assert.equal(opts.keepalive, true);
  assert.deepStrictEqual(JSON.parse(opts.body), { card: "c2", choice: "illegal" });

  const refused = () => Promise.resolve({ ok: false, status: 500 });
  assert.equal(await panel.saveAnswer(refused, "5", "c2", "illegal"), false);
  const lost = () => Promise.reject(new TypeError("network down"));
  assert.equal(await panel.saveAnswer(lost, "5", "c2", "illegal"), false);
});

test("resumeIndex returns to the remembered card, else the first unanswered one", () => {
  const answers = { c1: { choice: "legal" } };
  assert.equal(panel.resumeIndex(CARDS, answers, "c3"), 2);
  assert.equal(panel.resumeIndex(CARDS, answers, "c1"), 0);
  assert.equal(panel.resumeIndex(CARDS, answers, "gone-from-the-deck"), 1);
  assert.equal(panel.resumeIndex(CARDS, answers, null), 1);
});

// --- the panel itself, run against a small stand-in DOM ------------------------------------------

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "..", "sigint101", "static", "card-sort.js"), "utf8");

function fakeEl(attrs) {
  const classes = new Set();
  return {
    textContent: "",
    hidden: false,
    disabled: false,
    className: "",
    attrs: Object.assign({}, attrs),
    listeners: {},
    kids: {},
    classList: {
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); },
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
    },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    click() { if (!this.disabled) (this.listeners.click || []).forEach((fn) => fn({})); },
    querySelector(sel) { return this.kids[sel] || null; },
  };
}

function memoryStorage(seed) {
  const data = Object.assign({}, seed);
  return {
    data,
    getItem(k) { return k in data ? data[k] : null; },
    setItem(k, v) { data[k] = String(v); },
    removeItem(k) { delete data[k]; },
  };
}

function mountPanel({ saved = {}, fetchImpl, storage } = {}) {
  const els = {};
  for (const name of ["deck", "saved", "counter", "score", "unsaved", "text", "feedback", "tag",
    "answer", "why", "cite", "prev", "next"]) {
    els[name] = fakeEl({});
  }
  els.unsaved.hidden = true;
  els.deck.textContent = JSON.stringify(CARDS);
  els.saved.textContent = JSON.stringify(saved);
  const choices = ["legal", "illegal", "depends"].map((key) => {
    const btn = fakeEl({ "data-sort-choice": key });
    btn.kids["[data-sort-mark]"] = fakeEl({});
    return btn;
  });
  const panelEl = fakeEl({ "data-lesson-id": "5" });
  panelEl.kids = {
    "[data-sort-deck]": els.deck,
    "[data-sort-saved]": els.saved,
    "[data-sort-counter]": els.counter,
    "[data-sort-score]": els.score,
    "[data-sort-unsaved]": els.unsaved,
    "[data-sort-text]": els.text,
    "[data-sort-feedback]": els.feedback,
    "[data-sort-tag]": els.tag,
    "[data-sort-answer]": els.answer,
    "[data-sort-why]": els.why,
    "[data-sort-cite]": els.cite,
    "[data-sort-prev]": els.prev,
    "[data-sort-next]": els.next,
  };
  panelEl.querySelectorAll = (sel) => (sel === "[data-sort-choice]" ? choices : []);
  const calls = [];
  const context = {
    document: { querySelector: (sel) => (sel === "[data-sort-panel]" ? panelEl : null) },
    fetch: (url, opts) => { calls.push([url, opts]); return fetchImpl(url, opts); },
    console,
  };
  context.window = context;
  if (storage) context.sessionStorage = storage;
  vm.runInNewContext(SOURCE, context);
  const choose = (key) => choices.find((b) => b.getAttribute("data-sort-choice") === key).click();
  return { els, choices, calls, choose };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a tap posts with keepalive, so leaving for the law page at once still saves it", async () => {
  const ui = mountPanel({ fetchImpl: () => Promise.resolve({ ok: true }) });
  ui.choose("legal");
  assert.equal(ui.calls.length, 1);
  assert.equal(ui.calls[0][1].keepalive, true);
  await settle();
  assert.equal(ui.els.unsaved.hidden, true);
});

test("a refused or lost save shows a quiet not-saved mark until a later tap saves", async () => {
  let reply = () => Promise.resolve({ ok: false, status: 500 });
  const ui = mountPanel({ fetchImpl: () => reply() });
  ui.choose("legal");
  await settle();
  assert.equal(ui.els.unsaved.hidden, false, "a 500 must not pass silently");

  reply = () => Promise.reject(new TypeError("network down"));
  ui.choose("illegal");
  await settle();
  assert.equal(ui.els.unsaved.hidden, false);

  reply = () => Promise.resolve({ ok: true });
  ui.choose("legal");
  await settle();
  assert.equal(ui.els.unsaved.hidden, true);
});

test("the shown score follows the current answer key, like the per-card tag", () => {
  const ui = mountPanel({
    saved: { c1: { choice: "illegal", correct: true }, c2: { choice: "illegal", correct: true } },
    fetchImpl: () => Promise.resolve({ ok: true }),
  });
  assert.match(ui.els.score.textContent, /^Score 1\/2\b/);
});

test("coming back from the law page reopens the card the learner left, not the first unanswered", () => {
  const storage = memoryStorage();
  const fetchImpl = () => Promise.resolve({ ok: true });
  const first = mountPanel({ fetchImpl, storage });
  assert.equal(first.els.counter.textContent, "Card 1 of 3");
  first.els.next.click();
  first.els.next.click();
  assert.equal(first.els.counter.textContent, "Card 3 of 3");

  // A fresh page load in the same tab (Back from /law), with nothing answered yet.
  const back = mountPanel({ fetchImpl, storage });
  assert.equal(back.els.counter.textContent, "Card 3 of 3");

  // With session storage unusable the panel still opens, on the first unanswered card.
  const broken = {
    getItem() { throw new Error("storage blocked"); },
    setItem() { throw new Error("storage blocked"); },
  };
  const ui = mountPanel({
    saved: { c1: { choice: "legal", correct: true } },
    fetchImpl: () => Promise.resolve({ ok: true }),
    storage: broken,
  });
  assert.equal(ui.els.counter.textContent, "Card 2 of 3");
  ui.els.next.click();
  assert.equal(ui.els.counter.textContent, "Card 3 of 3");
});

test("formatFeedback returns Right or Missed with answer label, why, and cite", () => {
  const hit = panel.formatFeedback(CARDS[0], "legal");
  assert.deepStrictEqual(hit, {
    correct: true,
    tag: "Right",
    answerLabel: "Legal",
    why: "Open aviation traffic.",
    cite: "18 USC 2511(2)(g)(ii)(IV)",
  });

  const miss = panel.formatFeedback(CARDS[2], "legal");
  assert.deepStrictEqual(miss, {
    correct: false,
    tag: "Missed",
    answerLabel: "Depends",
    why: "Needs Part 15 compliance.",
    cite: "47 CFR 15.247",
  });
});
