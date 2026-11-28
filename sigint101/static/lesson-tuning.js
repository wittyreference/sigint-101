// ABOUTME: Puts the radios on the lesson's frequencies as the lesson page loads (POST /api/tuning),
// ABOUTME: without blocking the page: the panels' first polls wait for it, or for a 3 s timeout.

(function (root) {
  "use strict";

  // How long the panels wait for the restore before polling anyway. Retuning stops each moved
  // decoder, which is usually quick; a stuck tool can hold it for several seconds. The console
  // keeps that safe on its own (a poll's claim waits for the restore, and the restore never undoes
  // a newer tune), so this only decides when the panels ask and when the rail says "Tuning…".
  var TIMEOUT_MS = 3000;

  var script = document.currentScript;
  var lessonId = script && script.getAttribute("data-lesson-id");
  if (!lessonId) return;

  // The rail's note: hidden while all is well, so a restore that worked says nothing. It has two
  // short lines, the verdict and why, each read at a glance in the room the rail has left beside
  // "Locked until you predict" (about 14 characters), so no word depends on a hover.
  var note = document.querySelector("[data-lesson-tuning]");
  var head = note && note.querySelector("[data-lesson-tuning-head]");
  var why = note && note.querySelector("[data-lesson-tuning-why]");

  function say(verdict, reason) {
    if (!head || !why) return;
    head.textContent = verdict;
    why.textContent = reason || "";
    note.title = reason ? verdict + " · " + reason : verdict;
    note.hidden = false;
  }

  function clear() {
    if (!head || !why) return;
    head.textContent = "";
    why.textContent = "";
    note.title = "";
    note.hidden = true;
  }

  // Why the console refused: its own words when it gave any, else the status.
  function refusal(res) {
    return res.json().then(function (body) {
      var detail = body && body.detail;
      return typeof detail === "string" && detail ? detail : "error " + res.status;
    }, function () {
      return "error " + res.status;
    });
  }

  var answered = false;
  var open;
  var ready = new Promise(function (resolve) { open = resolve; });

  // The page goes on as soon as this tag has run; nothing here waits on the console. The panels
  // below hold their first poll on `ready`, which opens when the restore answers or, failing
  // that, at the timeout. A panel that polls after the timeout reads wherever its receiver really
  // is, and the note says the radios are still being tuned until the console answers.
  var timer = setTimeout(function () {
    if (!answered) say("Tuning…");
    open();
  }, TIMEOUT_MS);

  fetch("/api/tuning?lesson_id=" + encodeURIComponent(lessonId), { method: "POST", cache: "no-store" })
    .then(function (res) { return res.ok ? null : refusal(res); },
          function () { return "no console"; })
    .then(function (problem) {
      answered = true;
      clearTimeout(timer);
      if (problem) say("Not retuned", problem);
      else clear();
      open();
    });

  root.LessonTuning = { ready: ready };
})(this);
