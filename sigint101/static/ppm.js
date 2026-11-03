// ABOUTME: Drives the M4 ppm panel — the Measure button captures a station's 19 kHz stereo pilot
// ABOUTME: via /api/ppm/measure and shows the crystal error, or an honest "no pilot" when absent.

(function () {
  "use strict";

  // Loaded in node only for its pure helpers — the frequency to measure and the result text — with
  // no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { ppmResultText: ppmResultText, measureHz: measureHz };
    return;
  }

  // The station to measure: the frequency the radio is confirmed on (the dial's data-tuned-hz, set
  // by the waterfall only once the server confirms a span), else the panel's own station. The
  // dial's printed text is never read, since before confirmation it is only the lesson's target.
  function measureHz(tunedAttr, fallbackAttr) {
    var tuned = parseInt(tunedAttr, 10);
    if (isFinite(tuned) && tuned > 0) return tuned;
    var fallback = parseInt(fallbackAttr, 10);
    return isFinite(fallback) && fallback > 0 ? fallback : 98100000;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-ppm-panel]"));
  if (!panels.length) return;

  panels.forEach(function (panel) {
    var btn = panel.querySelector("[data-ppm-measure]");
    var result = panel.querySelector("[data-ppm-result]");
    var detail = panel.querySelector("[data-ppm-detail]");

    btn.addEventListener("click", function () {
      // Measure the station the learner has tuned to — a strong stereo one carries the pilot.
      var freqEl = panel.closest(".instrument").querySelector("[data-freq]");
      var hz = measureHz(freqEl && freqEl.getAttribute("data-tuned-hz"),
                         btn.getAttribute("data-ppm-hz"));
      btn.disabled = true;
      result.textContent = "measuring… (~8s)";
      result.className = "ppm__result";
      detail.textContent = "capturing the pilot at " + (hz / 1e6).toFixed(1) + " MHz";
      fetch("/api/ppm/measure?hz=" + hz, { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          var v = ppmResultText(body);
          result.textContent = v.text;
          result.className = "ppm__result ppm__result--" + v.kind;
          detail.textContent = v.detail;
        })
        .catch(function () {
          result.textContent = "measure failed";
          result.className = "ppm__result ppm__result--na";
          detail.textContent = "lost contact with the console";
        })
        .then(function () { btn.disabled = false; });
    });
  });

  // How a measurement reads to the learner: the signed ppm with its resolution when the pilot was
  // found, or an honest "no clear pilot" with the reason when it was not — never a guessed figure.
  function ppmResultText(r) {
    r = r || {};
    if (r.ppm === null || r.ppm === undefined) {
      return { text: "no clear pilot", kind: "na",
               detail: r.reason || "no 19 kHz pilot to measure against" };
    }
    var sign = r.ppm > 0 ? "+" : "";
    var res = (r.resolution_ppm !== null && r.resolution_ppm !== undefined)
      ? " ±" + r.resolution_ppm : "";
    return { text: sign + r.ppm + " ppm", kind: "ok",
             detail: "pilot " + (r.pilot_ratio_db === undefined ? "?" : r.pilot_ratio_db) +
                     " dB" + res };
  }
})();
