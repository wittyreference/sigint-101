// ABOUTME: Displays live or recorded rtl_433 rows for the ISM panel.
// ABOUTME: A mode change retires its old requests before they can paint or schedule another poll.

(function (root) {
  "use strict";

  // Under node's test runner the file is loaded for its pure formatters — the ones that turn a
  // decode into the line a learner reads — and there is no document. Export them and stop before
  // any DOM access; in the browser there is no `module`, so the panel runs instead.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      formatMHz: formatMHz, summarizeReadings: summarizeReadings, toneLabel: toneLabel,
      renderRow: renderRow,
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

  // Also used by other course screens that show a decoded row.
  function renderRow(doc, decode, replayMark) {
    var row = doc.createElement("li");
    row.className = "decoder__item decoder__item--" + (decode.mod === "FSK" ? "fsk" : "ask");
    if (replayMark) {
      row.className += " decoder__item--replay";
      row.appendChild(replayMark.content.cloneNode(true));
    }
    function append(kind, text) {
      var span = doc.createElement("span");
      span.className = "decoder__" + kind;
      span.textContent = text == null ? "" : String(text);
      row.appendChild(span);
    }
    append("model", decode.model);
    append("mod", decode.mod);
    append("read", summarizeReadings(decode.readings));
    append("tone", toneLabel(decode));
    return row;
  }

  root.DecodePanel = { renderRow: renderRow };

  // All instrument stages on this page share one decoder and one replay.
  var panels = Array.from(document.querySelectorAll("[data-decode-panel]"));
  if (!panels.length) return;
  var keys = Array.from(document.querySelectorAll("[data-replay-toggle]"));
  var mode = "live";
  var generation = 0;
  var busy = false;
  var recording = null;

  function updateKeys(body) {
    keys.forEach(function (key) {
      var strip = key.closest(".tune");
      var replaying = mode === "replay";
      var dial = strip.querySelector("[data-replay-dial]");
      strip.classList.toggle("tune--replay", replaying);
      key.disabled = busy || (!replaying && !recording);
      key.textContent = replaying ? "Back to live" : recording ? "Replay" : "No recording";
      key.title = !replaying && !recording ? "No recording on this kit yet" : "";
      if (replaying && body) {
        dial.querySelector("[data-replay-freq]").textContent =
          body.frequency_hz ? formatMHz(body.frequency_hz) + " MHz" : "— MHz";
        dial.querySelector("[data-replay-caption]").textContent =
          body.recording ? "REPLAY · " + body.recording : "REPLAY";
      }
    });
  }

  function read(gen, path, payload) {
    var init = { cache: "no-store" };
    if (payload !== undefined) {
      init.method = "POST";
      init.headers = { "content-type": "application/json" };
      init.body = JSON.stringify(payload);
    }
    return fetch(path, init).then(async function (reply) {
      var body = await reply.json();
      return gen === generation ? { ok: reply.ok, body: body } : null;
    });
  }

  async function listRecordings(gen) {
    var result = await read(gen, "/api/replay/recordings");
    if (result && result.ok) {
      recording = (result.body.recordings || []).find(function (item) {
        return item.instrument === "ism";
      });
      updateKeys();
    }
  }

  function replayNote(body) {
    if (body.reason) return body.reason;
    if (body.state === "finished") return "Replay finished · nothing decoded";
    if (body.state === "stopped") return "Replay stopped";
    return body.recording ? "Replaying recordings/" + body.recording + "…" : "Checking replay…";
  }

  function paint(body, ok) {
    var replaying = mode === "replay";
    if (replaying) updateKeys(body);
    panels.forEach(function (panel) {
      var list = panel.querySelector("[data-decode-list]");
      var empty = panel.querySelector("[data-decode-empty]");
      var badge = panel.querySelector("[data-decode-badge]");
      list.replaceChildren();
      if (!replaying && !ok) {
        DecoderPanel.showDown(panel, badge, empty, body);
        return;
      }
      var mark = replaying ? panel.querySelector("[data-replay-mark]") : null;
      var rows = (body.decodes || []).slice().reverse();
      rows.forEach(function (row) { list.appendChild(renderRow(document, row, mark)); });
      DecoderPanel.showLive(badge, replaying ? { mode: "replay" } : body);
      DecoderPanel.showCommand(panel, replaying && body.command && body.input
        ? body.command + " < " + body.input : body.command);
      empty.hidden = rows.length > 0 && !(replaying && body.reason);
      if (!empty.hidden) empty.textContent = replaying
        ? replayNote(body) : DecoderPanel.quietText(body, "nothing decoded yet");
    });
  }

  function switchMode(next) {
    mode = next;
    var gen = ++generation;
    updateKeys();
    paint(next === "replay" ? {} : { state: "starting" }, true);
    return gen;
  }

  function later(gen, delay) {
    setTimeout(function () {
      if (gen === generation) poll(gen);
    }, delay);
  }

  async function poll(gen) {
    var replaying = mode === "replay";
    try {
      var result = await read(gen, replaying ? "/api/replay/ism" : "/api/decode?limit=12");
      if (!result) return;
      if (!replaying && result.body.cause === "replaying") {
        poll(switchMode("replay"));
        return;
      }
      var ok = result.ok && result.body.available !== false;
      paint(result.body, ok);
      if (replaying && result.body.state === "replaying") later(gen, 500);
      if (!replaying) later(gen, ok ? 1500 : 5000);
    } catch (_) {
      if (gen !== generation) return;
      paint({ reason: "Lost contact with the console." }, false);
      later(gen, replaying ? 500 : 5000);
    }
  }

  async function toggle() {
    if (busy || (mode === "live" && !recording)) return;
    busy = true;
    var back = mode === "replay";
    var gen = switchMode("replay");
    try {
      if (!back) await listRecordings(gen);
      if (gen !== generation) return;
      if (!back && !recording) {
        poll(switchMode("live"));
        return;
      }
      var result = await read(gen, "/api/replay/ism/" + (back ? "stop" : "start"),
        back ? {} : { recording: recording.stem });
      if (!result) return;
      if (back && result.ok) {
        poll(switchMode("live"));
        return;
      }
      paint(result.body, result.ok);
      if (result.body.state === "replaying") later(gen, 500);
    } catch (_) {
      if (gen === generation) paint({ reason: "Lost contact with the console." }, false);
    } finally {
      busy = false;
      updateKeys();
    }
  }

  keys.forEach(function (key) { key.addEventListener("click", toggle); });
  listRecordings(generation).catch(function () {});
  var initial = generation;
  read(initial, "/api/replay/ism").then(function (result) {
    if (!result) return;
    if (result.body.state === "replaying") {
      var gen = switchMode("replay");
      paint(result.body, result.ok);
      later(gen, 500);
    } else {
      poll(initial);
    }
  }).catch(function () {
    if (generation === initial) poll(initial);
  });
})(this);
