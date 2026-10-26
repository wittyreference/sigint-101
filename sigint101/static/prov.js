// ABOUTME: Drives the M3 provenance panel by polling /api/provenance for the radio's own account
// ABOUTME: and the reference-vs-baseline verdict; the Capture button records a baseline sample.

(function () {
  "use strict";

  // Under node's test runner the file is loaded only for its pure decision — the verdict map — and
  // there is no document. Export it and stop before any DOM access, the same guard waterfall.js uses.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { provenanceVerdict: provenanceVerdict };
    return;
  }

  // Every instrument stage of an M3 lesson carries its own provenance panel (demo/try/observe), so
  // like the waterfall we drive them ALL from the one poll — the hidden stages stay in step and are
  // already correct when the learner steps to them.
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-prov-panel]"));
  if (!panels.length) return;

  var POLL_MS = 1500;
  var stopped = false;

  function fmtDb(v) { return (v === null || v === undefined) ? "—" : v.toFixed(1) + " dB"; }

  function renderPanel(panel, p, b) {
    var sel = function (s) { return panel.querySelector(s); };
    var v = provenanceVerdict(p, b);
    var verdict = sel("[data-prov-verdict]");
    verdict.textContent = v.text;
    verdict.className = "prov__verdict prov__verdict--" + v.kind;
    sel("[data-prov-ref]").textContent = fmtDb(p.ref_db);
    sel("[data-prov-base]").textContent = b.has_baseline ? fmtDb(b.baseline_ref_db) : "—";
    sel("[data-prov-gain]").textContent = (p.gain === null || p.gain === undefined) ? "auto" : p.gain;
    sel("[data-prov-temp]").textContent = (p.soc_temp_c === null || p.soc_temp_c === undefined)
      ? "—" : Math.round(p.soc_temp_c) + "°C";
    sel("[data-prov-power]").textContent = (p.throttled === null || p.throttled === undefined)
      ? "—" : (p.throttled.ok ? "OK" : "⚠ sag");
    sel("[data-prov-capture]").disabled = !(p.ref_db !== null && p.ref_db !== undefined);
  }

  function render(body) {
    var p = body.provenance || {};
    var b = body.baseline || {};
    panels.forEach(function (panel) { renderPanel(panel, p, b); });
  }

  function tick() {
    if (stopped) return;
    fetch("/api/provenance", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {
        panels.forEach(function (panel) {
          var verdict = panel.querySelector("[data-prov-verdict]");
          verdict.textContent = "NO CONSOLE";
          verdict.className = "prov__verdict prov__verdict--na";
        });
      })
      .then(function () { if (!stopped) setTimeout(tick, POLL_MS); });
  }

  panels.forEach(function (panel) {
    panel.querySelector("[data-prov-capture]").addEventListener("click", function () {
      fetch("/api/provenance/baseline", { method: "POST" })
        .then(function () { tick(); })
        .catch(function () { tick(); });
    });
  });

  tick();

  // The one decision the panel makes about what the learner sees. A deaf radio and a quiet band
  // draw the same waterfall, so only this reference-vs-baseline verdict tells them apart — stated
  // by word and glyph, with "no baseline" / "no radio" admitted plainly rather than guessed.
  function provenanceVerdict(p, b) {
    p = p || {};
    b = b || {};
    var hasRef = p.ref_db !== null && p.ref_db !== undefined;
    var hasRadio = hasRef || (p.soc_temp_c !== null && p.soc_temp_c !== undefined);
    if (!hasRadio) return { text: "NO RADIO", kind: "na" };
    if (!b.has_baseline) return { text: "NO BASELINE — capture one", kind: "na" };
    if (b.degraded) return { text: "▼ DEGRADED — gone deaf", kind: "bad" };
    return { text: "✓ HEALTHY", kind: "ok" };
  }
})();
