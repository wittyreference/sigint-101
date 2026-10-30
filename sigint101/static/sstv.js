// ABOUTME: Shows the newest decoded SSTV picture by polling /api/sstv (M25's picture-in-the-air lesson).
// ABOUTME: On 503 it shows the reason and never a placeholder picture — nothing decoded is honest.

(function () {
  "use strict";

  // The caption under a decoded picture: the SSTV mode sstv reported (when it did) and the time.
  function label(image) {
    image = image || {};
    var mode = image.mode || "unknown mode";
    var when = "";
    if (image.received_at != null) {
      var d = new Date(Number(image.received_at) * 1000);
      if (!isNaN(d.getTime())) when = d.toISOString().slice(11, 19) + " UTC";
    }
    return when ? mode + " · " + when : mode;
  }

  // Where the console serves the decoded PNG from — only /media exposes the image decoders' files.
  function src(image) {
    return "/media/" + ((image && image.path) || "");
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { label: label, src: src };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-sstv-panel]"));
  if (!panels.length) return;

  var POLL_MS = 2500;      // a picture takes ~1-2 min; poll steadily so it appears soon after it lands
  var RETRY_MS = 6000;
  var stopped = false;

  function render(panel, body) {
    var img = panel.querySelector("[data-sstv-img]");
    var cap = panel.querySelector("[data-sstv-cap]");
    var empty = panel.querySelector("[data-sstv-empty]");
    var badge = panel.querySelector("[data-sstv-badge]");
    var images = body.images || [];
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (images.length) {
      if (img) { img.src = src(images[0]); img.hidden = false; }
      if (cap) cap.textContent = label(images[0]);
      if (empty) empty.hidden = true;
    } else {
      if (img) { img.hidden = true; img.removeAttribute("src"); }
      if (cap) cap.textContent = "";
      if (empty) { empty.hidden = false; empty.textContent = "listening… no picture decoded yet"; }
    }
  }

  function showEmpty(panel, reason) {
    var img = panel.querySelector("[data-sstv-img]");
    var cap = panel.querySelector("[data-sstv-cap]");
    var empty = panel.querySelector("[data-sstv-empty]");
    var badge = panel.querySelector("[data-sstv-badge]");
    if (img) { img.hidden = true; img.removeAttribute("src"); }
    if (cap) cap.textContent = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode SSTV"; }
  }

  function tick() {
    fetch("/api/sstv?limit=1", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode SSTV";
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
