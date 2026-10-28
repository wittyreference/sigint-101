// ABOUTME: Lists live ACARS messages on the console by polling /api/acars (M16's aircraft lesson).
// ABOUTME: On 503 it shows the reason and never invents a message — absence is an honest answer.

(function () {
  "use strict";

  // Which aircraft sent the message: its registration and flight number, or a dash if neither.
  function ident(msg) {
    msg = msg || {};
    var parts = [];
    if (msg.tail) parts.push(msg.tail);
    if (msg.flight) parts.push(msg.flight);
    return parts.length ? parts.join(" ") : "—";
  }

  // The one line a learner reads: the message text, or a note for a text-less handshake.
  function summary(msg) {
    msg = msg || {};
    return msg.text ? msg.text : "(no text)";
  }

  // The frequency the message was heard on, to three places, or blank when unknown.
  function freq(msg) {
    return (msg && msg.freq_mhz != null) ? Number(msg.freq_mhz).toFixed(3) : "";
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { ident: ident, summary: summary, freq: freq };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-acars-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;      // ACARS messages arrive as one-off bursts; a steady poll keeps current
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-acars-list]");
    var empty = panel.querySelector("[data-acars-empty]");
    var badge = panel.querySelector("[data-acars-badge]");
    var messages = body.messages || [];
    list.innerHTML = "";
    messages.forEach(function (m) {
      var li = document.createElement("li");
      li.className = "decoder__item";
      li.innerHTML =
        '<span class="decoder__model">' + esc(ident(m)) + "</span>" +
        '<span class="decoder__mod">' + esc(m.label) + "</span>" +
        '<span class="decoder__read">' + esc(summary(m)) + "</span>" +
        '<span class="decoder__tone">' + esc(freq(m)) + "</span>";
      list.appendChild(li);
    });
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (empty) {
      empty.hidden = messages.length > 0;
      if (!messages.length) empty.textContent = "listening… no messages heard yet";
    }
  }

  function showEmpty(panel, reason) {
    var list = panel.querySelector("[data-acars-list]");
    var empty = panel.querySelector("[data-acars-empty]");
    var badge = panel.querySelector("[data-acars-badge]");
    if (list) list.innerHTML = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode ACARS"; }
  }

  function tick() {
    fetch("/api/acars?limit=12", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode ACARS";
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
