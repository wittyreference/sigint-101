// ABOUTME: Lists live rtl_433 decodes on the console by polling /api/decode (M10's FSK lesson).
// ABOUTME: On 503 it shows the reason and never invents a sensor read — absence is an honest answer.

(function () {
  "use strict";

  // Under node's test runner the file is loaded for its pure formatters — the ones that turn a
  // decode into the line a learner reads — and there is no document. Export them and stop before
  // any DOM access; in the browser there is no `module`, so the panel runs instead.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      formatMHz: formatMHz, summarizeReadings: summarizeReadings, toneLabel: toneLabel,
    };
    return;
  }

  // rtl_433 reports MHz to three places; the panel keeps that resolution so a learner can see the
  // two FSK tones differ by tens of kHz.
  function formatMHz(hz) {
    return (hz / 1e6).toFixed(3);
  }

  // The sensor's actual message, as a compact line. Temperature and humidity lead when present
  // (the weather-sensor case M10 uses); otherwise whatever fields the device reported, minus the
  // identity keys that are not a reading. Pure, so it is unit-tested without a DOM.
  function summarizeReadings(readings) {
    readings = readings || {};
    var parts = [];
    if ("temperature_C" in readings) parts.push(Number(readings.temperature_C).toFixed(1) + " °C");
    if ("humidity" in readings) parts.push(readings.humidity + "% RH");
    if (!parts.length) {
      Object.keys(readings).forEach(function (k) {
        if (k === "id" || k === "channel") return;   // identity, not a reading
        parts.push(k + " " + readings[k]);
      });
    }
    return parts.join(" · ");
  }

  // FSK rides two frequencies, so it is shown as a mark/space pair — the shape of the lesson.
  // ASK is one frequency switched on and off, so it shows a single tone.
  function toneLabel(decode) {
    if (decode.mod === "FSK" && decode.freq2_hz) {
      return formatMHz(decode.freq_hz) + " / " + formatMHz(decode.freq2_hz) + " MHz";
    }
    return formatMHz(decode.freq_hz) + " MHz";
  }

  // Every instrument stage of a decode lesson carries the same panel (one radio, one decoder), so
  // all of them are driven. No panel means this is not a decode lesson — nothing to do.
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-decode-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;      // rtl_433 reports a read only when a device transmits; no need to spin
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-decode-list]");
    var empty = panel.querySelector("[data-decode-empty]");
    var badge = panel.querySelector("[data-decode-badge]");
    var decodes = (body.decodes || []).slice().reverse();   // newest first
    list.innerHTML = "";
    decodes.forEach(function (d) {
      var li = document.createElement("li");
      li.className = "decoder__item decoder__item--" + (d.mod || "").toLowerCase();
      li.innerHTML =
        '<span class="decoder__model">' + esc(d.model) + "</span>" +
        '<span class="decoder__mod">' + esc(d.mod) + "</span>" +
        '<span class="decoder__read">' + esc(summarizeReadings(d.readings)) + "</span>" +
        '<span class="decoder__tone">' + esc(toneLabel(d)) + "</span>";
      list.appendChild(li);
    });
    if (badge) { badge.textContent = "LIVE"; badge.className = "decoder__status decoder__status--live"; }
    if (empty) {
      empty.hidden = decodes.length > 0;
      if (!decodes.length) empty.textContent = "listening… nothing decoded yet";
    }
  }

  function showEmpty(panel, reason) {
    var list = panel.querySelector("[data-decode-list]");
    var empty = panel.querySelector("[data-decode-empty]");
    var badge = panel.querySelector("[data-decode-badge]");
    if (list) list.innerHTML = "";
    if (badge) { badge.textContent = "NO RADIO"; badge.className = "decoder__status decoder__status--empty"; }
    if (empty) { empty.hidden = false; empty.textContent = reason || "no radio to decode"; }
  }

  function tick() {
    fetch("/api/decode?limit=12", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var waiting = false;
        if (!res.ok || res.body.available === false) {
          var reason = (res.body && res.body.reason) || "no radio to decode";
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
