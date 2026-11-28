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

  // A reason longer than the rail's line (the console's own words for a band it cannot take)
  // opens whole under the rail, where it wraps; a tap on it closes it, and a tap on the rail's
  // note opens it again. A reason that fits the rail opens nothing.
  var RAIL_CHARS = 14;
  var full = document.querySelector("[data-lesson-tuning-full]");
  var fullText = document.querySelector("[data-lesson-tuning-full-text]");
  var longReason = null;

  function showFull(open) {
    if (!full || !fullText) return;
    fullText.textContent = open ? longReason : "";
    full.hidden = !open;
  }

  if (full) full.addEventListener("click", function () { showFull(false); });
  if (note && note.addEventListener) {
    note.addEventListener("click", function () { if (longReason) showFull(true); });
  }

  function say(verdict, reason) {
    if (!head || !why) return;
    head.textContent = verdict;
    why.textContent = reason || "";
    note.title = reason ? verdict + " · " + reason : verdict;
    note.hidden = false;
    longReason = reason && reason.length > RAIL_CHARS ? reason : null;
    showFull(!!longReason);
  }

  function clear() {
    if (!head || !why) return;
    head.textContent = "";
    why.textContent = "";
    note.title = "";
    note.hidden = true;
    longReason = null;
    showFull(false);
  }

  // What the console's answer means for the rail, as [verdict, why], or null when every
  // receiver is on the lesson's frequency. A step the console skipped for a newer action is not
  // "done": the learner's own tune ("you retuned") or a newer lesson's restore. A refusal gives
  // the console's own reason: a band the sweep cannot take, a stale restore, or the status.
  function outcome(res) {
    return res.json().then(function (body) { return read(res, body || {}); },
                           function () { return read(res, {}); });
  }

  function read(res, body) {
    if (res.ok) {
      if (body.tuned !== false) return null;
      var why = Object.keys(body.skipped || {}).map(function (k) { return body.skipped[k]; });
      return ["Tuning skipped", why.indexOf("you retuned") >= 0 ? "you retuned" : "lesson changed"];
    }
    if (body.stale) return ["Not retuned", "lesson changed"];
    var reason = body.reason || body.detail;
    return ["Not retuned", typeof reason === "string" && reason ? reason : "error " + res.status];
  }

  // The learner's action, stamped on the console's clock (action-clock.js), so a request that
  // reaches the console late is ordered by when the learner opened the lesson.
  var stamp = root.ActionClock && root.ActionClock.stamp();
  var url = "/api/tuning?lesson_id=" + encodeURIComponent(lessonId) +
    (stamp ? "&epoch=" + encodeURIComponent(stamp.epoch) + "&at=" + stamp.at : "");

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

  fetch(url, { method: "POST", cache: "no-store" })
    .then(outcome, function () { return ["Not retuned", "no console"]; })
    .then(function (problem) {
      answered = true;
      clearTimeout(timer);
      if (problem) say(problem[0], problem[1]);
      else clear();
      open();
    });

  root.LessonTuning = { ready: ready };
})(this);
