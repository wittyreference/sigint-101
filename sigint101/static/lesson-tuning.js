// ABOUTME: Puts the radios on the lesson's frequencies as the lesson page loads (POST /api/tuning),
// ABOUTME: before the panel scripts below it start polling. Loading the page itself retunes nothing.

(function () {
  "use strict";

  var script = document.currentScript;
  var lessonId = script && script.getAttribute("data-lesson-id");
  if (!lessonId) return;

  // Synchronous on purpose: this tag sits ahead of every panel script, and the parser waits here,
  // so each panel's first poll finds its receiver already retuned (the GET that served the page
  // used to do this before rendering). A refusal or a lost console only costs the retune; the
  // panels still load and say what they find.
  var request = new XMLHttpRequest();
  request.open("POST", "/api/tuning?lesson_id=" + encodeURIComponent(lessonId), false);
  try {
    request.send();
  } catch (err) {
    /* the panels still run; they show whatever the receivers report */
  }
})();
