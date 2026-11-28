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

  // Each saved choice is scored against the deck's current answer key, the same way the per-card
  // tag is, so the tally and the tags never disagree if a card's key changes after it was sorted.
  // The stored `correct` flag is ignored here.
  function summarizeScore(cards, answers) {
    cards = Array.isArray(cards) ? cards : [];
    answers = answers && typeof answers === "object" ? answers : {};
    var answered = 0;
    var correct = 0;
    cards.forEach(function (card) {
      var entry = answers[card.key];
      if (entry && typeof entry.choice === "string") {
        answered += 1;
        if (entry.choice === card.answer) correct += 1;
      }
    });
    return { answered: answered, correct: correct, total: cards.length };
  }

  // Record one tap on the server. keepalive lets the write finish when the learner leaves at once
  // (Show me the law). Resolves true when saved, false when refused or lost; it never rejects.
  function saveAnswer(fetchFn, lessonId, cardKey, choice) {
    try {
      return Promise.resolve(fetchFn("/api/sort?lesson_id=" + encodeURIComponent(lessonId), {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "accept": "application/json",
        },
        body: JSON.stringify({ card: cardKey, choice: choice }),
        keepalive: true,
      })).then(function (response) {
        return !!(response && response.ok);
      }, function () { return false; });
    } catch (_) {
      return Promise.resolve(false);
    }
  }

  // The card to open on: the one the learner left in this tab (kept across the trip to /law),
  // else the first unanswered card.
  function resumeIndex(cards, answers, rememberedKey) {
    cards = Array.isArray(cards) ? cards : [];
    for (var i = 0; i < cards.length; i += 1) {
      if (rememberedKey && cards[i].key === rememberedKey) return i;
    }
    return firstUnansweredIndex(cards, answers);
  }

  // Session storage can be missing or throw (private windows, blocked site data); the place is a
  // convenience, so any failure just means opening on the first unanswered card. The place is
  // one-shot: set only on the way out to /law, and forgotten as soon as it is read back, so a later
  // ordinary visit in the same kiosk tab opens on the first unanswered card.
  function placeKey(lessonId) {
    return "sigint101.cardSort.place." + lessonId;
  }

  function recallCard(storage, lessonId) {
    var key = null;
    try { key = storage ? storage.getItem(placeKey(lessonId)) : null; } catch (_) { key = null; }
    try { if (storage && storage.removeItem) storage.removeItem(placeKey(lessonId)); } catch (_) { /* gone with the tab */ }
    return key;
  }

  function rememberCard(storage, lessonId, cardKey) {
    try { if (storage) storage.setItem(placeKey(lessonId), cardKey); } catch (_) { /* no place kept */ }
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
      saveAnswer: saveAnswer,
      resumeIndex: resumeIndex,
      recallCard: recallCard,
      rememberCard: rememberCard,
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
  var unsavedEl = panel.querySelector("[data-sort-unsaved]");
  var textEl = panel.querySelector("[data-sort-text]");
  var fbEl = panel.querySelector("[data-sort-feedback]");
  var tagEl = panel.querySelector("[data-sort-tag]");
  var ansEl = panel.querySelector("[data-sort-answer]");
  var whyEl = panel.querySelector("[data-sort-why]");
  var citeEl = panel.querySelector("[data-sort-cite]");
  var prevBtn = panel.querySelector("[data-sort-prev]");
  var nextBtn = panel.querySelector("[data-sort-next]");
  var choiceBtns = Array.prototype.slice.call(panel.querySelectorAll("[data-sort-choice]"));

  var store = null;
  try { store = window.sessionStorage || null; } catch (_) { store = null; }

  // Cards whose latest tap was refused or lost, and a per-card count so an older reply that lands
  // after a newer tap cannot overwrite the newer one's outcome.
  var unsaved = {};
  var tapSeq = {};

  var current = resumeIndex(cards, answers, recallCard(store, lessonId));

  function render() {
    var card = cards[current];
    var saved = answers[card.key] || null;
    var tally = summarizeScore(cards, answers);

    if (unsavedEl) unsavedEl.hidden = !unsaved[card.key];
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

    // The card steps keep the template's "Card" labels, set apart from the stage buttons; the
    // counter already says when this is the last card.
    if (prevBtn) prevBtn.disabled = current <= 0;
    if (nextBtn) nextBtn.disabled = current >= cards.length - 1;
  }

  function record(choice) {
    var card = cards[current];
    var fb = formatFeedback(card, choice);
    answers[card.key] = { choice: choice, correct: fb.correct };
    render();
    if (!lessonId) return;
    var seq = (tapSeq[card.key] || 0) + 1;
    tapSeq[card.key] = seq;
    // `fetch` is looked up at call time, so a render check that stubs window.fetch still holds.
    saveAnswer(function (url, opts) { return fetch(url, opts); }, lessonId, card.key, choice)
      .then(function (ok) {
        if (tapSeq[card.key] !== seq) return;
        if (ok) delete unsaved[card.key];
        else unsaved[card.key] = true;
        render();
      });
  }

  choiceBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      record(btn.getAttribute("data-sort-choice"));
    });
  });

  // Only the trip to "Show me the law" keeps the place; Back from there reopens this card.
  var lawLink = panel.querySelector("[data-sort-law]");
  if (lawLink) {
    lawLink.addEventListener("click", function () {
      rememberCard(store, lessonId, cards[current].key);
    });
  }

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
