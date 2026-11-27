// ABOUTME: Lists live LoRa receptions on the console by polling /api/lora (M11's lesson).
// ABOUTME: On 503 it shows the reason and never invents a packet — absence is an honest answer.

(function () {
  "use strict";

  // Under node's test runner the file is loaded for its pure formatters and there is no document.
  // Export them and stop before any DOM access; in the browser there is no `module`, so it runs.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { formatSnr, floorLabel, sendOutcome, radioLine };
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

  function sendOutcome(send) {
    if (send.status === "handed") return "handed to the radio";
    if (send.status === "repeated") return "a node repeated it";
    if (send.status === "no-repeat") return "No node repeated it. A mesh of one hears nothing.";
    return send.reason || "";
  }

  function radioLine(radio) {
    if (!radio || !radio.settings_read) return "Radio settings not reported";
    var power = radio.tx_power === 0 ? "radio default" :
      radio.tx_power == null ? "power not reported" : radio.tx_power + " dBm";
    var frequency = radio.freq_hz == null ? "frequency not reported" :
      radio.freq_hz / 1000000 + " MHz";
    return [radio.region, radio.preset == null ? "custom" : radio.preset, frequency, power]
      .filter(function (part) { return part != null; }).join(" · ");
  }

  // Every instrument stage of a LoRa lesson carries the same panel (one radio), so all are driven.
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-lora-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;      // packets arrive only when a node transmits; no need to spin
  var RETRY_MS = 5000;     // when meshtasticd is away, poll gently until it returns
  var stopped = false;
  var sendStrip = document.querySelector("[data-lora-send-permit]");
  var sendNumber = 0;
  var currentSend = null;

  function showOutcome(send) {
    var outcome = sendStrip.querySelector("[data-lora-outcome]");
    outcome.textContent = sendOutcome(send);
    outcome.dataset.state = send.status;
    outcome.hidden = false;
    sendStrip.querySelector("[data-lora-no-repeat]").hidden = send.status !== "no-repeat";
  }

  function showSendState(body) {
    if (!sendStrip) return;
    sendStrip.querySelector("[data-lora-radio]").textContent = radioLine(body.radio);
    var sends = body.sends || [];
    var send = sendNumber === 0 ? sends[sends.length - 1] :
      sends.find(function (row) {
        return currentSend && row.packet_id === currentSend.packet_id &&
          row.text === currentSend.text;
      });
    if (send) showOutcome(send);
  }

  if (sendStrip) {
    sendStrip.querySelector("[data-lora-send]").addEventListener("click", function () {
      var tap = ++sendNumber;
      currentSend = null;
      showOutcome({ status: "pending", reason: "Sending…" });
      fetch("/api/lora/send", {
        method: "POST", headers: { "X-LoRa-Send": sendStrip.dataset.loraSendPermit }
      }).then(function (response) { return response.json(); }).then(function (body) {
        if (tap !== sendNumber) return;
        currentSend = body.send || null;
        showOutcome(currentSend || { status: "error", reason: body.reason });
      }).catch(function () {
        if (tap === sendNumber) {
          showOutcome({ status: "error", reason: "Lost contact with the console." });
        }
      });
    });
  }

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
    DecoderPanel.showLive(badge, body);
    if (empty) {
      empty.hidden = packets.length > 0;
      if (!packets.length) empty.textContent = DecoderPanel.quietText(body, "no packets yet");
    }
  }

  function showEmpty(panel, body) {
    var list = panel.querySelector("[data-lora-list]");
    var empty = panel.querySelector("[data-lora-empty]");
    var badge = panel.querySelector("[data-lora-badge]");
    if (list) list.innerHTML = "";
    DecoderPanel.showDown(panel, badge, empty, body);
  }

  function tick() {
    fetch("/api/lora?limit=12", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        showSendState(res.body);
        if (!res.ok || res.body.available === false) {
          panels.forEach(function (p) { showEmpty(p, res.body); });
          waiting = true;
        } else {
          panels.forEach(function (p) { render(p, res.body); });
        }
        if (!stopped) setTimeout(tick, waiting ? RETRY_MS : POLL_MS);
      })
      .catch(function () {
        if (stopped) return;
        panels.forEach(function (p) { showEmpty(p, { reason: "Lost contact with the console." }); });
        setTimeout(tick, RETRY_MS);
      });
  }

  tick();
})();
