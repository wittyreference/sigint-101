// ABOUTME: Lists live LoRa receptions on the console by polling /api/lora (M11's lesson).
// ABOUTME: On 503 it shows the reason and never invents a packet — absence is an honest answer.

(function () {
  "use strict";

  // Under node's test runner the file is loaded for its pure formatters and there is no document.
  // Export them and stop before any DOM access; in the browser there is no `module`, so it runs.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { formatSnr: formatSnr, floorLabel: floorLabel };
    return;
  }

  // SNR to one place, always signed, so a learner reads -11.2 as plainly below zero.
  function formatSnr(snrDb) {
    if (snrDb === null || snrDb === undefined) return "— dB";
    var s = Number(snrDb).toFixed(1);
    return (snrDb > 0 ? "+" + s : s) + " dB";
  }

  // The lesson in two words: a negative SNR was decoded below the noise floor. Pure, node-tested.
  function floorLabel(packet) {
    return packet.below_floor ? "below floor" : "above floor";
  }

  // Every instrument stage of a LoRa lesson carries the same panel (one radio), so all are driven.
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-lora-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;      // packets arrive only when a node transmits; no need to spin
  var RETRY_MS = 5000;     // when meshtasticd is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-lora-list]");
    var empty = panel.querySelector("[data-lora-empty]");
    var badge = panel.querySelector("[data-lora-badge]");
    var packets = (body.packets || []).slice().reverse();   // newest first
    list.innerHTML = "";
    packets.forEach(function (p) {
      var li = document.createElement("li");
      li.className = "decoder__item" + (p.below_floor ? " decoder__item--floor" : "");
      li.innerHTML =
        '<span class="decoder__model">' + esc(p.from_id) + "</span>" +
        '<span class="decoder__mod">' + esc(formatSnr(p.snr_db)) + "</span>" +
        '<span class="decoder__read">' + esc(p.summary) + "</span>" +
        '<span class="decoder__tone">' + esc(floorLabel(p)) + "</span>";
      list.appendChild(li);
    });
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (empty) {
      empty.hidden = packets.length > 0;
      if (!packets.length) empty.textContent = "listening… no packets yet";
    }
  }

  function showEmpty(panel, reason) {
    var list = panel.querySelector("[data-lora-list]");
    var empty = panel.querySelector("[data-lora-empty]");
    var badge = panel.querySelector("[data-lora-badge]");
    if (list) list.innerHTML = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no LoRa radio to read"; }
  }

  function tick() {
    fetch("/api/lora?limit=12", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no LoRa radio to read";
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
