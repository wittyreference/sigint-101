// ABOUTME: Lists live POCSAG pages on the console by polling /api/pocsag (M15's pager lesson).
// ABOUTME: On 503 it shows the reason and never invents a page — absence is an honest answer.

(function () {
  "use strict";

  // The short tag for a page's content type, shown beside its address.
  function kindLabel(kind) {
    if (kind === "alpha") return "ALPHA";
    if (kind === "numeric") return "NUM";
    if (kind === "skyper") return "SKYPER";
    return "TONE";
  }

  // The pager's cap code (address), or a dash when none decoded.
  function addr(page) {
    return (page && page.address != null) ? String(page.address) : "—";
  }

  // The one line a learner reads for a page: its message, or a note for a tone-only page.
  function summary(page) {
    page = page || {};
    if (page.text) return page.text;
    return page.kind === "tone" ? "(tone only)" : "";
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { kindLabel: kindLabel, addr: addr, summary: summary };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-pocsag-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;      // pages arrive as one-off bursts; a steady poll keeps the log current
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-pocsag-list]");
    var empty = panel.querySelector("[data-pocsag-empty]");
    var badge = panel.querySelector("[data-pocsag-badge]");
    var pages = body.pages || [];
    list.innerHTML = "";
    pages.forEach(function (p) {
      var li = document.createElement("li");
      li.className = "decoder__item decoder__item--" + (p.kind || "tone");
      li.innerHTML =
        '<span class="decoder__model">' + esc(addr(p)) + "</span>" +
        '<span class="decoder__mod">' + esc(kindLabel(p.kind)) + "</span>" +
        '<span class="decoder__read">' + esc(summary(p)) + "</span>" +
        '<span class="decoder__tone">' + esc(p.baud) + " bd</span>";
      list.appendChild(li);
    });
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (empty) {
      empty.hidden = pages.length > 0;
      if (!pages.length) empty.textContent = "listening… no pages heard yet";
    }
  }

  function showEmpty(panel, reason) {
    var list = panel.querySelector("[data-pocsag-list]");
    var empty = panel.querySelector("[data-pocsag-empty]");
    var badge = panel.querySelector("[data-pocsag-badge]");
    if (list) list.innerHTML = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode POCSAG"; }
  }

  function tick() {
    fetch("/api/pocsag?limit=12", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode POCSAG";
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
