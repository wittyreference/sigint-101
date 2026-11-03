// ABOUTME: The rack's own wiring: the step key, a decoder's frequency override through
// ABOUTME: /api/<decoder>/tune, and the one-line reception log read from /api/receptions.

(function () {
  "use strict";

  // A step as the server renders it: kHz below a megahertz, MHz from there, no trailing zeros.
  function formatStep(hz) {
    return hz >= 1e6 ? String(hz / 1e6) + " MHz" : String(hz / 1e3) + " kHz";
  }

  function parseSteps(text) {
    return String(text || "").split(",").map(function (s) { return parseInt(s, 10); })
      .filter(function (n) { return isFinite(n) && n > 0; });
  }

  // The step after `current` in the instrument's list, wrapping to the first.
  function nextStep(steps, current) {
    var i = steps.indexOf(current);
    return i === -1 ? steps[0] : steps[(i + 1) % steps.length];
  }

  function formatDecoderMHz(hz, pager) {
    return (hz / 1e6).toFixed(pager ? 4 : 3);
  }

  // What the decoder dial's caption says: on the commercial paging band the pager shows framing
  // only (the decoder withholds the words there; this only says so), else the decoder's band.
  function tuneCaption(hz, strip) {
    if (strip.withheldLow && hz >= strip.withheldLow && hz <= strip.withheldHigh) {
      return "framing only · text withheld";
    }
    return strip.bandName;
  }

  // Gathers +/- taps on a decoder's dial into one retune at a time, counted from the frequency
  // the console last confirmed. A tool restarts on every retune, so a burst of taps costs one.
  //   opts.current()   -> the confirmed frequency
  //   opts.stepHz()    -> the step, read at each tap (the step key changes it)
  //   opts.send(hz)    -> Promise of {tuned, freq_hz} or {tuned: false, reason}
  //   opts.onPending(hz), opts.onResult(res)
  function makeStepper(opts) {
    var steps = 0;
    var timer = null;
    var busy = false;

    function target() {
      return opts.current() + steps * opts.stepHz();
    }

    function arm() {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(flush, opts.delayMs);
      opts.onPending(target());
    }

    function flush() {
      timer = null;
      if (busy) return;               // sent once the request in flight settles
      if (!steps) { opts.onPending(null); return; }
      var hz = target();
      steps = 0;
      busy = true;
      opts.send(hz).then(function (res) {
        busy = false;
        opts.onResult(res);
        if (steps) arm();
        else opts.onPending(null);
      });
    }

    return { step: function (n) { steps += n; arm(); } };
  }

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  // Where a reception was heard: one frequency, both channels of a two-channel decoder, or
  // nothing for a radio that picks its own channel.
  function freqLabel(freq) {
    if (typeof freq === "number") return (freq / 1e6).toFixed(3) + " MHz";
    if (Array.isArray(freq) && freq.length) {
      return freq.map(function (hz) { return (hz / 1e6).toFixed(3); }).join("/") + " MHz";
    }
    return "";
  }

  function receptionLine(event) {
    var d = new Date(event.ts * 1000);
    var when = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
    var p = event.payload || {};
    return [when, p.tool, freqLabel(p.freq_hz), p.summary]
      .filter(function (part) { return part; }).join(" · ");
  }

  function logQuery(instrument) {
    return "/api/receptions?limit=1" + (instrument ? "&instrument=" + instrument : "");
  }

  function logText(body) {
    if (body.error) return body.error;
    var newest = (body.receptions || [])[0];
    return newest ? receptionLine(newest) : "no receptions logged yet";
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      formatStep: formatStep, parseSteps: parseSteps, nextStep: nextStep,
      formatDecoderMHz: formatDecoderMHz, tuneCaption: tuneCaption, makeStepper: makeStepper,
      freqLabel: freqLabel, receptionLine: receptionLine, logQuery: logQuery, logText: logText,
    };
    return;
  }

  var bench = document.querySelector(".rack__bench");
  if (!bench) return;

  var TUNE_DELAY_MS = 450;   // taps closer together than this become one retune
  var LOG_POLL_MS = 3000;

  // The picker scrolls on its own; keep the instrument on the bench in view when the page opens.
  var current = document.querySelector(".rack__pick[aria-current]");
  if (current && current.scrollIntoView) current.scrollIntoView({ block: "nearest" });

  // ---- The step key: rewrites the strip's data-step-hz, which the +/- keys read at each tap ----
  var stepKey = bench.querySelector("[data-step-cycle]");
  var strip = bench.querySelector("[data-tune-strip], [data-decoder-tune]");
  if (stepKey && strip) {
    var steps = parseSteps(stepKey.getAttribute("data-steps"));
    var label = stepKey.querySelector("[data-step-label]");
    stepKey.addEventListener("click", function () {
      var next = nextStep(steps, parseInt(strip.getAttribute("data-step-hz"), 10));
      strip.setAttribute("data-step-hz", String(next));
      if (label) label.textContent = formatStep(next);
    });
  }

  // ---- A decoder's frequency override ----------------------------------------------------------
  var tuneStrip = bench.querySelector("[data-decoder-tune]");
  if (tuneStrip) {
    var api = tuneStrip.getAttribute("data-decoder-tune");
    var freqEl = tuneStrip.querySelector("[data-decoder-freq]");
    var caption = tuneStrip.querySelector("[data-tune-caption]");
    var pager = tuneStrip.hasAttribute("data-withheld-low");
    var info = {
      bandName: caption ? caption.getAttribute("data-band-name") : "",
      withheldLow: parseInt(tuneStrip.getAttribute("data-withheld-low"), 10) || 0,
      withheldHigh: parseInt(tuneStrip.getAttribute("data-withheld-high"), 10) || 0,
    };
    var confirmed = parseInt(tuneStrip.getAttribute("data-freq-hz"), 10);
    var refusal = null;

    var showDial = function (hz, pending) {
      if (freqEl) {
        freqEl.innerHTML = formatDecoderMHz(hz, pager) + "<span>MHz</span>";
        freqEl.classList.toggle("tune__freq--pending", !!pending);
      }
    };
    var showCaption = function (text) {
      if (caption) { caption.textContent = text; caption.title = text; }
    };

    var stepper = makeStepper({
      delayMs: TUNE_DELAY_MS,
      current: function () { return confirmed; },
      stepHz: function () { return parseInt(tuneStrip.getAttribute("data-step-hz"), 10) || 0; },
      send: function (hz) {
        return fetch("/api/" + api + "/tune", {
          method: "POST", cache: "no-store",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ freq_hz: hz }),
        }).then(function (res) { return res.json(); })
          .catch(function (err) {
            return { tuned: false, reason: "lost contact with the console: " + err.message };
          });
      },
      onPending: function (hz) {
        if (hz === null) {
          showDial(confirmed, false);
          showCaption(refusal || tuneCaption(confirmed, info));
        } else {
          showDial(hz, true);
          showCaption("tuning to " + formatDecoderMHz(hz, pager) + " MHz…");
        }
      },
      onResult: function (res) {
        if (res.tuned) { confirmed = res.freq_hz; refusal = null; }
        else refusal = "not tuned: " + (res.reason || "refused");
      },
    });

    tuneStrip.querySelectorAll("[data-tune-step]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        refusal = null;
        stepper.step(parseInt(btn.getAttribute("data-tune-step"), 10) || 0);
      });
    });
  }

  // ---- The reception log: the newest reception, logged with its provenance -----------------
  var log = bench.querySelector("[data-rack-log]");
  if (log) {
    var line = log.querySelector("[data-rack-log-line]");
    var query = logQuery(log.getAttribute("data-instrument"));
    var poll = function () {
      fetch(query, { cache: "no-store" })
        .then(function (res) { return res.json(); })
        .then(function (body) {
          var text = logText(body);
          line.textContent = text;
          line.title = text;
        })
        .catch(function () { line.textContent = "lost contact with the console"; })
        .then(function () { setTimeout(poll, LOG_POLL_MS); });
    };
    poll();
  }
})();
