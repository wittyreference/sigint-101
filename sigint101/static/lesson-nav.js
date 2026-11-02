// ABOUTME: Lesson-stage navigation: Next, Previous and rail-pip jumps between the rendered panels,
// ABOUTME: each step remembered server-side (POST /api/stage) so a reload resumes; Complete leads on.

(function () {
  "use strict";

  // Pure: the panel index a step of `delta` from `cur` lands on among `count` panels, or -1 when it
  // would fall off either end (no Previous before the first stage; Next on the last completes).
  function stepTo(cur, delta, count) {
    var to = cur + delta;
    return (to >= 0 && to < count) ? to : -1;
  }

  // Pure: how one rail pip reads, from its index in the stage order, the open stage's index, the
  // furthest index reached, and whether it is sealed (a stage past predict, before the commit).
  //   "locked" sealed: never tappable     "active" the open stage
  //   "done"   reached before: tappable   "ahead"  not reached yet: disabled
  function pipState(i, current, furthest, sealed) {
    if (sealed) return "locked";
    if (i === current) return "active";
    return i <= furthest ? "done" : "ahead";
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { stepTo: stepTo, pipState: pipState };
    return;
  }

  var stageEl = document.querySelector(".stage");
  if (!stageEl) return;
  var lessonId = stageEl.getAttribute("data-lesson-id");
  // Only the panels the server rendered: sealed stages are absent until the commit.
  var panels = Array.prototype.slice.call(document.querySelectorAll(".stage-panel"));
  // One pip per stage in the lesson's order; a sealed one is a <span>, never a button.
  var pips = Array.prototype.slice.call(document.querySelectorAll(".rail-pip"));
  var order = pips.map(function (p) { return p.getAttribute("data-stage"); });
  var furthest = parseInt(stageEl.getAttribute("data-reached"), 10) || 0;

  function openIndex() {
    return panels.findIndex(function (p) { return !p.hidden; });
  }

  function paintRail(current) {
    pips.forEach(function (pip, i) {
      var state = pipState(i, current, furthest, pip.tagName !== "BUTTON");
      pip.classList.toggle("rail-pip--active", state === "active");
      pip.classList.toggle("rail-pip--done", state === "done");
      if (pip.tagName === "BUTTON") pip.disabled = (state === "ahead");
      if (state === "active") pip.setAttribute("aria-current", "step");
      else pip.removeAttribute("aria-current");
    });
  }

  // The server keeps the place so a reload or a kiosk restart resumes here. Fire-and-forget: the
  // step itself never waits on it, and a refusal only costs the resume, never the lesson.
  function remember(stage) {
    if (!lessonId) return;
    fetch("/api/stage?lesson_id=" + encodeURIComponent(lessonId), {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: "stage=" + encodeURIComponent(stage),
      keepalive: true
    }).catch(function () { /* the step already happened on screen */ });
  }

  function show(idx) {
    if (idx < 0 || idx >= panels.length) return;
    panels.forEach(function (p, i) { p.hidden = (i !== idx); });
    var stage = panels[idx].getAttribute("data-stage");
    var current = order.indexOf(stage);
    furthest = Math.max(furthest, current);
    paintRail(current);
    remember(stage);
  }

  // Finish the lesson: record completion, then lead on — the next lesson in the unit, or the unit
  // page at its end (the server put that on the Complete button). On any refusal still lead on, so
  // the learner is never trapped on the last screen (worst case the lesson stays un-ticked).
  function complete() {
    var btn = document.querySelector("[data-complete]");
    var next = (btn && btn.getAttribute("data-next-href")) || "/";
    if (!lessonId) { window.location = next; return; }
    fetch("/api/complete?lesson_id=" + encodeURIComponent(lessonId), { method: "POST" })
      .then(function () { window.location = next; })
      .catch(function () { window.location = next; });
  }

  document.querySelectorAll("[data-nav='next']").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var cur = openIndex();
      var to = stepTo(cur, 1, panels.length);
      if (to > -1) show(to);
      else if (cur === panels.length - 1) complete();   // last stage: Next finishes the lesson
    });
  });
  document.querySelectorAll("[data-nav='prev']").forEach(function (btn) {
    btn.addEventListener("click", function () {
      show(stepTo(openIndex(), -1, panels.length));
    });
  });
  document.querySelectorAll("[data-nav='goto']").forEach(function (pip) {
    pip.addEventListener("click", function () {
      var stage = pip.getAttribute("data-stage");
      show(panels.findIndex(function (p) { return p.getAttribute("data-stage") === stage; }));
    });
  });
  document.querySelectorAll("[data-complete]").forEach(function (btn) {
    btn.addEventListener("click", complete);
  });
})();
