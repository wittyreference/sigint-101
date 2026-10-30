// ABOUTME: Shows the newest decoded NOAA APT weather image by polling /api/apt (M24's space lesson).
// ABOUTME: On 503 it shows the reason and never a placeholder picture — no pass is an honest answer.

(function () {
  "use strict";

  // The caption under a decoded image: the satellite it came from and when it was received.
  function label(image) {
    image = image || {};
    var sat = image.satellite || "NOAA";
    var when = image.received_at ? String(image.received_at).slice(0, 16).replace("T", " ") : "";
    return when ? sat + " · " + when : sat;
  }

  // Where the console serves the decoded PNG from — the image decoders write under data/, which
  // only /media exposes (and only .png files there).
  function src(image) {
    return "/media/" + ((image && image.path) || "");
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { label: label, src: src };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-apt-panel]"));
  if (!panels.length) return;

  var POLL_MS = 4000;      // a pass is slow (~15 min); the image appears once, so poll gently
  var RETRY_MS = 6000;
  var stopped = false;

  function render(panel, body) {
    var img = panel.querySelector("[data-apt-img]");
    var cap = panel.querySelector("[data-apt-cap]");
    var empty = panel.querySelector("[data-apt-empty]");
    var badge = panel.querySelector("[data-apt-badge]");
    var images = body.images || [];
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (images.length) {
      if (img) { img.src = src(images[0]); img.hidden = false; }
      if (cap) cap.textContent = label(images[0]);
      if (empty) empty.hidden = true;
    } else {
      if (img) { img.hidden = true; img.removeAttribute("src"); }
      if (cap) cap.textContent = "";
      if (empty) { empty.hidden = false; empty.textContent = "listening… no pass decoded yet"; }
    }
  }

  function showEmpty(panel, reason) {
    var img = panel.querySelector("[data-apt-img]");
    var cap = panel.querySelector("[data-apt-cap]");
    var empty = panel.querySelector("[data-apt-empty]");
    var badge = panel.querySelector("[data-apt-badge]");
    if (img) { img.hidden = true; img.removeAttribute("src"); }
    if (cap) cap.textContent = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode APT"; }
  }

  function tick() {
    fetch("/api/apt?limit=1", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode APT";
          panels.forEach(function (p) { showEmpty(p, reason); });
          waiting = true;
        } else {
          panels.forEach(function (p) { render(p, res.body); });
        }
        if (!stopped) setTimeout(tick, waiting ? RETRY_MS : POLL_MS);
      })
      .catch(function () { if (!stopped) setTimeout(tick, RETRY_MS); });
  }

  tick();
})();
