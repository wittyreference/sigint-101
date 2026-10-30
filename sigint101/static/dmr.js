// ABOUTME: Lists live DMR/P25 calls on the console by polling /api/dmr (M27's digital-voice lesson).
// ABOUTME: On 503 it shows the reason and never invents a call — a quiet channel is an honest answer.

(function () {
  "use strict";

  // How a call reads as a line: the protocol, and the DMR time-slot when the frame named one.
  function ident(call) {
    call = call || {};
    return call.slot != null ? call.protocol + " · slot " + call.slot : call.protocol;
  }

  // Who is talking: the talkgroup (or called unit) and the source radio, each shown when decoded.
  function parties(call) {
    call = call || {};
    var parts = [];
    if (call.talkgroup != null) parts.push("TG " + call.talkgroup);
    if (call.source != null) parts.push("SRC " + call.source);
    return parts.join(" · ");
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { ident: ident, parties: parties };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-dmr-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;      // digital voice arrives as one-off calls; a steady poll keeps the log current
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-dmr-list]");
    var empty = panel.querySelector("[data-dmr-empty]");
    var badge = panel.querySelector("[data-dmr-badge]");
    var calls = body.calls || [];
    list.innerHTML = "";
    calls.forEach(function (c) {
      var li = document.createElement("li");
      li.className = "decoder__item";
      li.innerHTML =
        '<span class="decoder__model">' + esc(ident(c)) + "</span>" +
        '<span class="decoder__mod">' + esc(c.kind || "") + "</span>" +
        '<span class="decoder__read">' + esc(parties(c)) + "</span>" +
        '<span class="decoder__tone">' + esc(c.last_seen || "") + "</span>";
      list.appendChild(li);
    });
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (empty) {
      empty.hidden = calls.length > 0;
      if (!calls.length) empty.textContent = "listening… no calls heard yet";
    }
  }

  function showEmpty(panel, reason) {
    var list = panel.querySelector("[data-dmr-list]");
    var empty = panel.querySelector("[data-dmr-empty]");
    var badge = panel.querySelector("[data-dmr-badge]");
    if (list) list.innerHTML = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode DMR/P25"; }
  }

  function tick() {
    fetch("/api/dmr?limit=12", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode DMR/P25";
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
