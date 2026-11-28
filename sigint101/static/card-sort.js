// ABOUTME: Drives the one-card-at-a-time legal card sort inside a lesson's try stage (M5).
// ABOUTME: Records each tap via POST /api/sort, shows why + citation, and never locks Next step.

(function () {
  "use strict";

  var SORT_CHOICES = Object.freeze(["legal", "illegal", "depends"]);

  function choiceLabel(key) {
    if (key === "legal") return "Legal";
    if (key === "illegal") return "Illegal";
    if (key === "depends") return "Depends";
    return "—";
  }

  function summarizeScore(cards, answers) {
    cards = Array.isArray(cards) ? cards : [];
    answers = answers && typeof answers === "object" ? answers : {};
    var answered = 0;
    var correct = 0;
    cards.forEach(function (card) {
      var entry = answers[card.key];
      if (entry && typeof entry.choice === "string") {
        answered += 1;
        if (entry.correct) correct += 1;
      }
    });
    return { answered: answered, correct: correct, total: cards.length };
  }

  function firstUnansweredIndex(cards, answers) {
    cards = Array.isArray(cards) ? cards : [];
    answers = answers && typeof answers === "object" ? answers : {};
    for (var i = 0; i < cards.length; i += 1) {
      if (!answers[cards[i].key]) return i;
    }
    return 0;
  }

  function stepCard(index, delta, total) {
    var next = index + delta;
    return (next >= 0 && next < total) ? next : -1;
  }

  function formatFeedback(card, choice) {
    card = card || {};
    var isCorrect = choice === card.answer;
    return {
      correct: isCorrect,
      tag: isCorrect ? "Right" : "Missed",
      answerLabel: choiceLabel(card.answer),
      why: card.why || "",
      cite: card.cite || "",
    };
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      SORT_CHOICES: SORT_CHOICES,
      choiceLabel: choiceLabel,
      summarizeScore: summarizeScore,
      firstUnansweredIndex: firstUnansweredIndex,
      stepCard: stepCard,
      formatFeedback: formatFeedback,
    };
    return;
  }

  var panel = document.querySelector("[data-sort-panel]");
  if (!panel) return;

  var deckEl = panel.querySelector("[data-sort-deck]");
  var savedEl = panel.querySelector("[data-sort-saved]");
  if (!deckEl) return;

  var cards = [];
  var answers = {};
  try { cards = JSON.parse(deckEl.textContent || "[]"); } catch (_) { cards = []; }
  try { answers = JSON.parse((savedEl && savedEl.textContent) || "{}"); } catch (_) { answers = {}; }
  if (!cards.length) return;

  var lessonId = panel.getAttribute("data-lesson-id") || "";
  var counterEl = panel.querySelector("[data-sort-counter]");
  var scoreEl = panel.querySelector("[data-sort-score]");
  var textEl = panel.querySelector("[data-sort-text]");
  var fbEl = panel.querySelector("[data-sort-feedback]");
  var tagEl = panel.querySelector("[data-sort-tag]");
  var ansEl = panel.querySelector("[data-sort-answer]");
  var whyEl = panel.querySelector("[data-sort-why]");
  var citeEl = panel.querySelector("[data-sort-cite]");
  var prevBtn = panel.querySelector("[data-sort-prev]");
  var nextBtn = panel.querySelector("[data-sort-next]");
  var choiceBtns = Array.prototype.slice.call(panel.querySelectorAll("[data-sort-choice]"));

  var current = firstUnansweredIndex(cards, answers);

  function render() {
    var card = cards[current];
    var saved = answers[card.key] || null;
    var tally = summarizeScore(cards, answers);

    if (counterEl) counterEl.textContent = "Card " + (current + 1) + " of " + cards.length;
    if (scoreEl) {
      scoreEl.textContent = "Score " + tally.correct + "/" + tally.answered +
        " (" + tally.answered + "/" + tally.total + ")";
    }
    if (textEl) textEl.textContent = card.text;

    choiceBtns.forEach(function (btn) {
      var key = btn.getAttribute("data-sort-choice");
      var picked = !!(saved && saved.choice === key);
      var isRight = !!(saved && card.answer === key);
      btn.classList.toggle("sort-btn--picked", picked);
      btn.classList.toggle("sort-btn--answer", isRight);
      btn.setAttribute("aria-pressed", picked ? "true" : "false");
      var mark = btn.querySelector("[data-sort-mark]");
      if (mark) mark.textContent = isRight ? "✓ " : (picked ? "• " : "");
    });

    if (fbEl) {
      if (saved) {
        var fb = formatFeedback(card, saved.choice);
        fbEl.hidden = false;
        fbEl.className = "sort-feedback verdict verdict--" + (fb.correct ? "hit" : "miss");
        if (tagEl) tagEl.textContent = fb.tag;
        if (ansEl) ansEl.textContent = fb.answerLabel + ":";
        if (whyEl) whyEl.textContent = fb.why;
        if (citeEl) citeEl.textContent = fb.cite;
      } else {
        fbEl.hidden = true;
      }
    }

    if (prevBtn) prevBtn.disabled = current <= 0;
    if (nextBtn) {
      nextBtn.disabled = current >= cards.length - 1;
      nextBtn.textContent = current >= cards.length - 1 ? "Last card" : "Next card";
    }
  }

  function record(choice) {
    var card = cards[current];
    var fb = formatFeedback(card, choice);
    answers[card.key] = { choice: choice, correct: fb.correct };
    render();
    if (!lessonId) return;
    fetch("/api/sort?lesson_id=" + encodeURIComponent(lessonId), {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "accept": "application/json",
      },
      body: JSON.stringify({ card: card.key, choice: choice }),
    }).catch(function () { /* feedback is already shown on screen */ });
  }

  choiceBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      record(btn.getAttribute("data-sort-choice"));
    });
  });

  if (prevBtn) {
    prevBtn.addEventListener("click", function () {
      var to = stepCard(current, -1, cards.length);
      if (to > -1) { current = to; render(); }
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener("click", function () {
      var to = stepCard(current, 1, cards.length);
      if (to > -1) { current = to; render(); }
    });
  }

  render();
})();
