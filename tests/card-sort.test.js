// ABOUTME: Tests the card-sort panel's pure helpers: choice labels, score tally, card stepping,
// ABOUTME: resume index, and feedback formatting. Runs under node's built-in test runner.

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
