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

  // Past the timeout the rail says what the console is doing: a restore held up by a ppm
  // calibration (which holds the radio for its capture) is "measuring", never "Tuning…". The
  // console's gate state is a read-only GET, asked again every WAIT_POLL_MS while this restore is
  // pending, so the rail follows the calibration's end; it stops once the restore answers.
  var WAIT_POLL_MS = 1000;

  function sayWaiting(isCurrent) {
    say("Tuning…");
    (function ask() {
      if (!isCurrent()) return;
      fetch("/api/tuning", { cache: "no-store" })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (state) {
          if (!isCurrent()) return;
          if (state && state.waiting_on === "calibration") say("Radio busy", "measuring ppm");
          else say("Tuning…");
          setTimeout(ask, WAIT_POLL_MS);
        }, function () {
          if (isCurrent()) setTimeout(ask, WAIT_POLL_MS);
        });
    })();
  }

  var open;
  var ready = new Promise(function (resolve) { open = resolve; });
  var latest = 0;

  // One restore: the learner's action, stamped on the console's clock (action-clock.js), so a
  // request that reaches the console late is ordered by when the learner opened the lesson. The
  // page goes on as soon as it is sent; nothing here waits on the console. The panels below hold
  // their first poll on `ready`, which opens when the restore answers or, failing that, at the
  // timeout. Only the newest restore's answer is shown.
  function restore(opening) {
    var run = ++latest;
    var current = function () { return run === latest; };
    var answered = false;
    // The first restore is the learner's act of opening the lesson, so it is stamped at the
    // page's navigation start, however late this script ran; a later one (a bfcache show) now.
    var clock = root.ActionClock;
    var stamp = clock && (opening && clock.navigationStamp ? clock.navigationStamp() : clock.stamp());
    var url = "/api/tuning?lesson_id=" + encodeURIComponent(lessonId) +
      (stamp ? "&epoch=" + encodeURIComponent(stamp.epoch) + "&at=" + stamp.at : "");

    var timer = setTimeout(function () {
      if (!answered && current()) sayWaiting(function () { return !answered && current(); });
      open();
    }, TIMEOUT_MS);

    fetch(url, { method: "POST", cache: "no-store" })
      .then(outcome, function () { return ["Not retuned", "no console"]; })
      .then(function (problem) {
        answered = true;
        clearTimeout(timer);
        if (current()) {
          if (problem) say(problem[0], problem[1]);
          else clear();
        }
        open();
      });
  }

  restore(true);

  // Shown again from the back/forward cache (Back, after another lesson retuned the radios): the
  // page is as it was left, with no load to post its restore, so it posts it again. Its clock
  // still maps the browser's timeline onto the console's (both ran on while it was frozen), so
  // the stamp taken now is current, and the restore is newer than the other lesson's.
  if (root.addEventListener) {
    root.addEventListener("pageshow", function (event) {
      if (event && event.persisted) restore(false);
    });
  }

  root.LessonTuning = { ready: ready };
})(this);
