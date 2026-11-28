// ABOUTME: Puts the radios on the lesson's frequencies as the lesson page loads (POST /api/tuning),
// ABOUTME: without blocking the page: the panels' first polls wait for it, or for a 3 s timeout.

(function (root) {
  "use strict";

  // How long the panels wait for the restore before polling anyway. Retuning stops and restarts
  // each moved decoder, which is usually quick; a stuck tool can hold it for several seconds, and
  // a panel that waited that long would show nothing at all.
  var TIMEOUT_MS = 3000;

  var script = document.currentScript;
  var lessonId = script && script.getAttribute("data-lesson-id");
  if (!lessonId) return;

  // The rail's note: empty and hidden while all is well, so a restore that worked says nothing.
  var note = document.querySelector("[data-lesson-tuning]");

  function say(text) {
    if (!note) return;
    note.textContent = text;
    note.title = text;
    note.hidden = false;
  }

  function clear() {
    if (!note) return;
    note.textContent = "";
    note.title = "";
    note.hidden = true;
  }

  // Why the console refused: its own words when it gave any, else the status.
  function refusal(res) {
    return res.json().then(function (body) {
      var detail = body && body.detail;
      return typeof detail === "string" && detail ? detail : "refused (" + res.status + ")";
    }, function () {
      return "refused (" + res.status + ")";
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
    if (!answered) say("Tuning radios…");
    open();
  }, TIMEOUT_MS);

  fetch("/api/tuning?lesson_id=" + encodeURIComponent(lessonId), { method: "POST", cache: "no-store" })
    .then(function (res) { return res.ok ? null : refusal(res); },
          function () { return "lost contact with the console"; })
    .then(function (problem) {
      answered = true;
      clearTimeout(timer);
      // The verdict leads, so it shows even when the rail has room for only a few words.
      if (problem) say("Not retuned · " + problem);
      else clear();
      open();
    });

  root.LessonTuning = { ready: ready };
})(this);
