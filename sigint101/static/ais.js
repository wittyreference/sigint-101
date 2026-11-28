// ABOUTME: Lists live AIS vessels on the console by polling /api/ais (M26's ship-tracking lesson).
// ABOUTME: On 503 it shows the reason and never invents a vessel — an empty sea is an honest answer.

(function () {
  "use strict";

  // Marine VHF AIS channels sit at 161.975 and 162.025 MHz; tuning within 100 kHz of 162.0 MHz
  // lands the learner on the AIS band for faded consolidation lessons.
  var AIS_BAND_LOW_HZ = 161900000;
  var AIS_BAND_HIGH_HZ = 162100000;
  var TUNER_MIN_HZ = 24000000;
  var TUNER_MAX_HZ = 1766000000;

  // How a vessel reads as a line: its name once the static message arrives, else its MMSI.
  function ident(vessel) {
    vessel = vessel || {};
    return vessel.name ? vessel.name : "MMSI " + vessel.mmsi;
  }

  // The position the vessel last reported, to four places, or a note while only static data is in.
  function position(vessel) {
    vessel = vessel || {};
    if (vessel.lat == null || vessel.lon == null) return "no position yet";
    return Number(vessel.lat).toFixed(4) + ", " + Number(vessel.lon).toFixed(4);
  }

  // The way it is making: speed over ground and course, each shown only when the ship reported it.
  function motion(vessel) {
    vessel = vessel || {};
    var parts = [];
    if (vessel.sog_kt != null) parts.push(Number(vessel.sog_kt).toFixed(1) + " kt");
    if (vessel.cog_deg != null) parts.push(Number(vessel.cog_deg).toFixed(0) + "°");
    return parts.join(" · ");
  }

  function inAisBand(freqHz) {
    return typeof freqHz === "number" && freqHz >= AIS_BAND_LOW_HZ && freqHz <= AIS_BAND_HIGH_HZ;
  }

  function formatFreqMHz(freqHz) {
    return (Number(freqHz) / 1e6).toFixed(1);
  }

  function formatStepLabel(stepHz) {
    if (stepHz >= 1000000) return (stepHz / 1000000) + " MHz";
    return Math.round(stepHz / 1000) + " kHz";
  }

  function stepFadedFreq(freqHz, dir, stepHz) {
    var next = freqHz + dir * stepHz;
    if (next < TUNER_MIN_HZ) return TUNER_MIN_HZ;
    if (next > TUNER_MAX_HZ) return TUNER_MAX_HZ;
    return next;
  }

  function nextFadedStep(steps, currentStepHz) {
    var idx = steps.indexOf(currentStepHz);
    return steps[(idx + 1) % steps.length];
  }

  // Honest state for a faded AIS panel before and after the learner taps Tune.
  // Never claims "Tuned" before the receiver confirms: AIS listens on fixed marine VHF channels,
  // so the prompt names what the learner chose ("Set to ...") and keeps confirmed false until
  // /api/ais confirms a live receiver.
  function fadedTuneState(freqHz, attempted) {
    if (!attempted) {
      return {
        tuned: false,
        confirmed: false,
        badge: "UNTUNED",
        cls: "decoder__status decoder__status--empty",
        message: "Not tuned yet — step to marine VHF (162 MHz) and tap Tune.",
      };
    }
    if (inAisBand(freqHz)) {
      return {
        tuned: true,
        confirmed: false,
        badge: "CHECKING",
        cls: "decoder__status",
        message: "Set to " + formatFreqMHz(freqHz) + " MHz (marine VHF) — checking receiver…",
      };
    }
    return {
      tuned: false,
      confirmed: false,
      badge: "OFF BAND",
      cls: "decoder__status decoder__status--empty",
      message: formatFreqMHz(freqHz) + " MHz is outside the marine VHF band (~162 MHz). Step to ~162 MHz and tap Tune.",
    };
  }

  // F1: moving the dial after any attempted Tune (accepted or rejected) drops back to UNTUNED
  // and unconfirmed so stale refusals or live rosters never stay beside an uncommitted dial.
  function onFadedDialMove(freqHz, hadAttempt) {
    if (hadAttempt) {
      return {
        active: false,
        attempted: false,
        confirmed: false,
        state: fadedTuneState(freqHz, false),
      };
    }
    return { active: false, attempted: false, confirmed: false, state: null };
  }

  // F1b: a settling receiver ("starting") can still fail, so keep the dial pending until the
  // console confirms state === "running".
  function isAisReceiverConfirmed(body) {
    return Boolean(body && body.available !== false && body.state === "running");
  }

  // F2: keep refusal badges and retry times honest across every cause while replacing raw server
  // diagnostics (binary names, capability flags, deploy/provision.sh) with learner-facing copy.
  function scrubFadedRefusal(body) {
    var src = body || {};
    var out = {};
    if (typeof src.available === "boolean") out.available = src.available;
    if (typeof src.cause === "string") out.cause = src.cause;
    if (typeof src.state === "string") out.state = src.state;
    if (typeof src.retry_in === "number") out.retry_in = src.retry_in;

    if (out.cause === "stopped" || out.state === "stopped") {
      out.reason = "The marine VHF receiver stopped unexpectedly.";
      return out;
    }
    if (out.cause === "missing_tool") {
      out.message = "The marine VHF receiver is not installed on this unit.";
      out.reason = out.message;
      out.next_step = "See the Field card for receiver setup.";
      return out;
    }
    if (out.cause === "no_radio") {
      out.message = "No RTL-SDR radio detected.";
      out.reason = out.message;
      out.next_step = "Plug in the RTL-SDR.";
      return out;
    }
    if (out.cause === "replaying") {
      out.message = "The radio is busy with a replay.";
      out.reason = out.message;
      out.next_step = "Stop the replay, then tap Tune again.";
      return out;
    }
    if (src.reason === "Lost contact with the console.") {
      out.reason = src.reason;
      return out;
    }
    out.message = "The marine VHF receiver is unavailable right now.";
    out.reason = out.message;
    out.next_step = "See the Field card if this persists.";
    return out;
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      AIS_BAND_LOW_HZ: AIS_BAND_LOW_HZ,
      AIS_BAND_HIGH_HZ: AIS_BAND_HIGH_HZ,
      ident: ident,
      position: position,
      motion: motion,
      inAisBand: inAisBand,
      fadedTuneState: fadedTuneState,
      onFadedDialMove: onFadedDialMove,
      isAisReceiverConfirmed: isAisReceiverConfirmed,
      scrubFadedRefusal: scrubFadedRefusal,
      stepFadedFreq: stepFadedFreq,
      nextFadedStep: nextFadedStep,
    };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-ais-panel]"));
  if (!panels.length) return;

  var livePanels = panels.filter(function (p) { return !p.hasAttribute("data-ais-faded"); });
  var fadedPanels = panels.filter(function (p) { return p.hasAttribute("data-ais-faded"); });

  var POLL_MS = 1500;      // vessels beacon on a schedule; a steady poll keeps the roster current
  var RETRY_MS = 5000;     // when the radio is away, poll gently until it returns
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-ais-list]");
    var empty = panel.querySelector("[data-ais-empty]");
    var badge = panel.querySelector("[data-ais-badge]");
    var vessels = body.vessels || [];
    list.innerHTML = "";
    vessels.forEach(function (v) {
      var li = document.createElement("li");
      li.className = "decoder__item";
      li.innerHTML =
        '<span class="decoder__model">' + esc(ident(v)) + "</span>" +
        '<span class="decoder__mod">' + esc(v.nav_status || "") + "</span>" +
        '<span class="decoder__read">' + esc(position(v)) + "</span>" +
        '<span class="decoder__tone">' + esc(motion(v)) + "</span>";
      list.appendChild(li);
    });
    DecoderPanel.showLive(badge, body);
    DecoderPanel.showCommand(panel, body.command);
    if (empty) {
      empty.hidden = vessels.length > 0;
      if (!vessels.length) empty.textContent = DecoderPanel.quietText(body, "no vessels heard yet");
    }
  }

  function showEmpty(panel, body) {
    var list = panel.querySelector("[data-ais-list]");
    var empty = panel.querySelector("[data-ais-empty]");
    var badge = panel.querySelector("[data-ais-badge]");
    if (list) list.innerHTML = "";
    DecoderPanel.showDown(panel, badge, empty, body);
  }

  function startLivePoll(targets) {
    function tick() {
      fetch("/api/ais?limit=12", { cache: "no-store" })
        .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
        .then(function (res) {
          var waiting = false;
          if (!res.ok || res.body.available === false) {
            targets.forEach(function (p) { showEmpty(p, res.body); });
            waiting = true;
          } else {
            targets.forEach(function (p) { render(p, res.body); });
          }
          if (!stopped) setTimeout(tick, waiting ? RETRY_MS : POLL_MS);
        })
        .catch(function () {
          if (stopped) return;
          targets.forEach(function (p) { showEmpty(p, { reason: "Lost contact with the console." }); });
          setTimeout(tick, RETRY_MS);
        });
    }
    tick();
  }

  if (livePanels.length) {
    startLivePoll(livePanels);
  }

  // Faded consolidation lesson: stay quiet and honest until the learner tunes to marine VHF.
  // Note (N1): polling is gated client-side here; GET /api/ais itself remains the live endpoint.
  if (fadedPanels.length) {
    var strips = Array.prototype.slice.call(document.querySelectorAll('[data-faded-tune="ais"]'));
    var first = strips[0];
    var steps = first && first.dataset.steps
      ? first.dataset.steps.split(",").map(function (s) { return parseInt(s, 10); }).filter(Boolean)
      : [10000000, 1000000, 100000];
    var freqHz = first && first.dataset.freqHz ? parseInt(first.dataset.freqHz, 10) : 100000000;
    var stepHz = first && first.dataset.stepHz ? parseInt(first.dataset.stepHz, 10) : steps[0];
    var tunedInBand = false;
    var tuneAttempted = false;
    var receiverConfirmed = false;
    var pollTimer = null;
    var pollGen = 0;

    function syncStrips() {
      strips.forEach(function (strip) {
        strip.dataset.freqHz = String(freqHz);
        strip.dataset.stepHz = String(stepHz);
        var freqEl = strip.querySelector("[data-faded-freq]");
        if (freqEl) {
          freqEl.innerHTML = formatFreqMHz(freqHz) + "<span>MHz</span>";
          freqEl.classList.toggle("tune__freq--pending", !receiverConfirmed);
        }
        var stepEl = strip.querySelector("[data-faded-step-val]");
        if (stepEl) stepEl.textContent = formatStepLabel(stepHz);
      });
    }

    function applyFadedState(state) {
      fadedPanels.forEach(function (panel) {
        var list = panel.querySelector("[data-ais-list]");
        var empty = panel.querySelector("[data-ais-empty]");
        var badge = panel.querySelector("[data-ais-badge]");
        if (list) list.innerHTML = "";
        if (badge) {
          badge.textContent = state.badge;
          badge.className = state.cls;
        }
        if (empty) {
          empty.hidden = false;
          empty.textContent = state.message;
        }
      });
    }

    function stopFadedPoll() {
      pollGen += 1;
      if (pollTimer !== null) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function startFadedPoll() {
      stopFadedPoll();
      var gen = pollGen;
      function tickFaded() {
        if (stopped || !tunedInBand || gen !== pollGen) return;
        fetch("/api/ais?limit=12", { cache: "no-store" })
          .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
          .then(function (res) {
            if (stopped || !tunedInBand || gen !== pollGen) return;
            var waiting = false;
            if (!res.ok || res.body.available === false) {
              receiverConfirmed = false;
              syncStrips();
              var scrubbed = scrubFadedRefusal(res.body);
              fadedPanels.forEach(function (p) { showEmpty(p, scrubbed); });
              waiting = true;
            } else {
              receiverConfirmed = isAisReceiverConfirmed(res.body);
              syncStrips();
              fadedPanels.forEach(function (p) { render(p, res.body); });
            }
            if (!stopped && tunedInBand && gen === pollGen) {
              pollTimer = setTimeout(tickFaded, waiting ? RETRY_MS : POLL_MS);
            }
          })
          .catch(function () {
            if (stopped || !tunedInBand || gen !== pollGen) return;
            receiverConfirmed = false;
            syncStrips();
            fadedPanels.forEach(function (p) {
              showEmpty(p, scrubFadedRefusal({ reason: "Lost contact with the console." }));
            });
            pollTimer = setTimeout(tickFaded, RETRY_MS);
          });
      }
      tickFaded();
    }

    strips.forEach(function (strip) {
      Array.prototype.forEach.call(strip.querySelectorAll("[data-faded-dir]"), function (btn) {
        btn.addEventListener("click", function () {
          var dir = parseInt(btn.dataset.fadedDir, 10) || 0;
          freqHz = stepFadedFreq(freqHz, dir, stepHz);
          var moved = onFadedDialMove(freqHz, tunedInBand || tuneAttempted);
          if (tunedInBand) stopFadedPoll();
          tunedInBand = moved.active;
          tuneAttempted = moved.attempted;
          receiverConfirmed = moved.confirmed;
          syncStrips();
          if (moved.state) applyFadedState(moved.state);
        });
      });
      var stepBtn = strip.querySelector("[data-faded-step]");
      if (stepBtn) {
        stepBtn.addEventListener("click", function () {
          stepHz = nextFadedStep(steps, stepHz);
          syncStrips();
        });
      }
      var applyBtn = strip.querySelector("[data-faded-apply]");
      if (applyBtn) {
        applyBtn.addEventListener("click", function () {
          var state = fadedTuneState(freqHz, true);
          tuneAttempted = true;
          if (state.tuned) {
            tunedInBand = true;
            receiverConfirmed = Boolean(state.confirmed);
            syncStrips();
            applyFadedState(state);
            startFadedPoll();
          } else {
            tunedInBand = false;
            receiverConfirmed = false;
            stopFadedPoll();
            syncStrips();
            applyFadedState(state);
          }
        });
      }
    });

    syncStrips();
  }

  window.addEventListener("pagehide", function () { stopped = true; });
})();
