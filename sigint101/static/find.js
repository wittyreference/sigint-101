// ABOUTME: Drives the Tier B live-find unit tile and challenge screen via /api/challenges/<n>.
// ABOUTME: Polls every 2 s, ticks the countdown each second, and drops stale pre-Start polls.

(function () {
  "use strict";

  var MARKS = {
    idle: { word: "READY", cls: "find-mark find-mark--idle" },
    active: { word: "HUNTING", cls: "find-mark find-mark--active" },
    passed: { word: "FOUND", cls: "find-mark find-mark--passed" },
    expired: { word: "TIME UP", cls: "find-mark find-mark--expired" },
  };

  var START_LABELS = {
    idle: "Start",
    active: "Restart",
    passed: "Try again",
    expired: "Start again",
  };

  var INSTRUMENT_LABELS = {
    ism: "ISM",
    rds: "RDS",
    lora: "LoRa",
    adsb: "ADS-B",
    aprs: "APRS",
    pager: "POCSAG",
    acars: "ACARS",
    ais: "AIS",
    dmr: "DMR/P25",
  };

  function markFor(state) {
    return MARKS[state] || MARKS.idle;
  }

  function startLabel(state) {
    return START_LABELS[state] || START_LABELS.idle;
  }

  function clock(remainingSeconds) {
    var raw = Number(remainingSeconds);
    var secs = !isFinite(raw) || raw <= 0 ? 0 : Math.floor(raw);
    var mins = Math.floor(secs / 60);
    var rem = secs % 60;
    return (mins < 10 ? "0" : "") + mins + ":" + (rem < 10 ? "0" : "") + rem;
  }

  function formatFreq(freqHz) {
    if (Array.isArray(freqHz)) {
      var parts = freqHz
        .filter(function (hz) { return typeof hz === "number" && hz > 0; })
        .map(function (hz) { return Number((hz / 1e6).toFixed(3)).toString(); });
      return parts.length ? parts.join(" / ") + " MHz" : "";
    }
    if (typeof freqHz === "number" && isFinite(freqHz) && freqHz > 0) {
      return Number((freqHz / 1e6).toFixed(3)).toString() + " MHz";
    }
    return "";
  }

  function formatUtc(ts) {
    if (typeof ts !== "number" || !isFinite(ts)) return "";
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return "";
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return (
      d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) +
      " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()) +
      " UTC"
    );
  }

  function passParts(latest) {
    var inst = INSTRUMENT_LABELS[latest.instrument] || String(latest.instrument || "").toUpperCase();
    var parts = [inst];
    if (latest.tool) parts.push(String(latest.tool));
    var freq = formatFreq(latest.freq_hz);
    if (freq) parts.push(freq);
    var when = formatUtc(latest.passed_at);
    if (when) parts.push(when);
    return parts.join(" · ");
  }

  function evidenceLine(body) {
    if (!body || typeof body !== "object") return "";
    if (body.error) return String(body.error);
    if (body.state === "passed" && body.latest_pass) {
      return "Found: " + passParts(body.latest_pass);
    }
    if (body.reason === "clock_moved") {
      return "The clock changed. Start again.";
    }
    if (body.latest_pass) {
      return "Found before: " + passParts(body.latest_pass);
    }
    return "";
  }

  var api = {
    markFor: markFor,
    startLabel: startLabel,
    clock: clock,
    evidenceLine: evidenceLine,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof window !== "undefined") {
    window.LiveFind = api;
  }
  if (typeof document === "undefined") {
    return;
  }

  function bindWidget(el, isScreen) {
    var unitId = el.getAttribute("data-unit-id");
    if (!unitId) return;
    var markEl = el.querySelector("[data-find-mark]");
    var clockEl = el.querySelector("[data-find-clock]");
    var startBtn = isScreen ? el.querySelector("[data-find-start]") : null;
    var evidenceEl = isScreen ? el.querySelector("[data-find-evidence]") : null;

    var generation = 0;
    var currentState = "idle";
    var remaining = 0;

    function render(body) {
      if (!body || typeof body !== "object") return;
      currentState = body.state || "idle";
      remaining = Math.max(0, Number(body.remaining_s) || 0);
      var mark = markFor(currentState);
      if (markEl) {
        markEl.textContent = mark.word;
        markEl.className = mark.cls;
      }
      if (clockEl) {
        clockEl.textContent = clock(remaining);
        if (!isScreen) {
          clockEl.hidden = currentState !== "active";
        }
      }
      if (startBtn) {
        startBtn.textContent = startLabel(currentState);
      }
      if (evidenceEl) {
        evidenceEl.textContent = evidenceLine(body);
      }
    }

    function schedulePoll() {
      setTimeout(poll, 2000);
    }

    function poll() {
      var myGen = generation;
      fetch("/api/challenges/" + unitId)
        .then(function (resp) {
          if (!resp || !resp.ok) return null;
          return resp.json();
        })
        .then(function (body) {
          if (myGen !== generation) return;
          if (body) render(body);
        })
        .catch(function () { /* keep last known state until next poll */ })
        .finally(schedulePoll);
    }

    if (startBtn) {
      startBtn.addEventListener("click", function () {
        generation += 1;
        var postGen = generation;
        fetch("/api/challenges/" + unitId + "/start", { method: "POST" })
          .then(function (resp) {
            if (!resp) return null;
            return resp.json().catch(function () { return null; });
          })
          .then(function (body) {
            if (postGen !== generation) return;
            if (body) render(body);
          })
          .catch(function () { /* next poll will refresh */ });
      });
    }

    setInterval(function () {
      if (currentState !== "active") return;
      remaining = Math.max(0, remaining - 1);
      if (clockEl) {
        clockEl.textContent = clock(remaining);
      }
    }, 1000);

    poll();
  }

  var screen = document.querySelector("[data-find-screen]");
  if (screen) bindWidget(screen, true);

  var tiles = document.querySelectorAll("[data-find-tile]");
  for (var i = 0; i < tiles.length; i += 1) {
    bindWidget(tiles[i], false);
  }
})();
