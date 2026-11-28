// ABOUTME: Drives Tier C solo unit tiles and the Hunt -> Step A -> Step B -> Result screen.
// ABOUTME: Reuses LiveFind.clock, keeps its own 6-state mark map, and enforces leak-free pre-pass copy.

(function () {
  "use strict";

  var MARKS = {
    idle: { word: "READY", cls: "solo-mark solo-mark--idle" },
    active: { word: "HUNTING", cls: "solo-mark solo-mark--active" },
    reception_passed: { word: "CALL", cls: "solo-mark solo-mark--reception_passed" },
    passed: { word: "PASSED", cls: "solo-mark solo-mark--passed" },
    call_missed: { word: "MISSED", cls: "solo-mark solo-mark--call_missed" },
    expired: { word: "TIME UP", cls: "solo-mark solo-mark--expired" },
    unreadable: { word: "UNREADABLE", cls: "solo-mark solo-mark--unreadable" },
  };

  var START_LABELS = {
    idle: "Start",
    active: "Restart",
    reception_passed: "Restart",
    passed: "Try again",
    call_missed: "Try again",
    expired: "Start again",
  };

  var CALL_LABELS = {
    legal: "Legal to listen",
    illegal: "Not legal",
    not_sure: "Not sure",
  };

  function markFor(state) {
    return MARKS[state] || MARKS.idle;
  }

  function startLabel(state) {
    return START_LABELS[state] || START_LABELS.idle;
  }

  function callLabel(call) {
    return CALL_LABELS[call] || String(call || "");
  }

  function clock(remainingSeconds) {
    if (typeof window !== "undefined" && window.LiveFind && typeof window.LiveFind.clock === "function") {
      return window.LiveFind.clock(remainingSeconds);
    }
    var raw = Number(remainingSeconds);
    var secs = !isFinite(raw) || raw <= 0 ? 0 : Math.floor(raw);
    var mins = Math.floor(secs / 60);
    var rem = secs % 60;
    return (mins < 10 ? "0" : "") + mins + ":" + (rem < 10 ? "0" : "") + rem;
  }

  function huntEvidenceLine(body) {
    if (!body || typeof body !== "object") return "";
    if (body.error) return String(body.error);
    if (body.reason === "clock_moved") return "The clock changed. Start again.";
    if (body.passed_before_date) return "Passed before \u00b7 " + String(body.passed_before_date);
    return "";
  }

  function initialCallState() {
    return { step: "a", call: null, whyKey: null };
  }

  function stepTransition(ui, action) {
    var cur = ui || initialCallState();
    if (!action || typeof action !== "object") return cur;
    if (action.type === "pick_call") {
      return { step: "b", call: action.call, whyKey: null };
    }
    if (action.type === "pick_why") {
      return { step: "b", call: cur.call, whyKey: action.whyKey };
    }
    if (action.type === "change_call" || action.type === "reset") {
      return initialCallState();
    }
    return cur;
  }

  function canSubmitCall(ui) {
    return Boolean(ui && ui.step === "b" && ui.call && ui.whyKey);
  }

  var api = {
    markFor: markFor,
    startLabel: startLabel,
    callLabel: callLabel,
    clock: clock,
    huntEvidenceLine: huntEvidenceLine,
    initialCallState: initialCallState,
    stepTransition: stepTransition,
    canSubmitCall: canSubmitCall,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof window !== "undefined") {
    window.SoloUI = api;
  }
  if (typeof document === "undefined") {
    return;
  }

  function bindTile(el) {
    var soloId = el.getAttribute("data-solo-id");
    if (!soloId) return;
    var markEl = el.querySelector("[data-solo-mark]");
    var clockEl = el.querySelector("[data-solo-clock]");
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
        clockEl.hidden = currentState !== "active";
      }
    }

    function schedule() {
      setTimeout(poll, 2000);
    }

    function poll() {
      fetch("/api/solos/" + encodeURIComponent(soloId))
        .then(function (resp) { return resp && resp.ok ? resp.json() : null; })
        .then(function (body) { if (body) render(body); })
        .catch(function () { /* keep last state */ })
        .finally(schedule);
    }

    setInterval(function () {
      if (currentState !== "active") return;
      remaining = Math.max(0, remaining - 1);
      if (clockEl) clockEl.textContent = clock(remaining);
    }, 1000);

    poll();
  }

  function activeCallLeg(body) {
    if (!body || !Array.isArray(body.legs)) return null;
    for (var i = 0; i < body.legs.length; i += 1) {
      if (body.legs[i].passed && !body.legs[i].called) return body.legs[i];
    }
    return null;
  }

  function latestResultLeg(body) {
    if (!body || !Array.isArray(body.legs)) return null;
    var chosen = null;
    for (var i = 0; i < body.legs.length; i += 1) {
      if (body.legs[i].call_result) {
        chosen = body.legs[i];
        if (!body.legs[i].call_result.legal_passed) return body.legs[i];
      }
    }
    return chosen;
  }

  function bindScreen(el) {
    var soloId = el.getAttribute("data-solo-id");
    var backSelf = el.getAttribute("data-solo-back") || "/";
    if (!soloId) return;

    var views = {
      hunt: el.querySelector('[data-solo-view="hunt"]'),
      "call-a": el.querySelector('[data-solo-view="call-a"]'),
      "call-b": el.querySelector('[data-solo-view="call-b"]'),
      result: el.querySelector('[data-solo-view="result"]'),
    };
    var markEl = el.querySelector("[data-solo-mark]");
    var clockEl = el.querySelector("[data-solo-clock]");
    var startBtn = el.querySelector("[data-solo-start]");
    var evidenceEl = el.querySelector("[data-solo-evidence]");
    var heardEl = el.querySelector("[data-solo-heard]");
    var legalPromptEl = el.querySelector("[data-solo-legal-prompt]");
    var callErrorEl = el.querySelector("[data-solo-call-error]");
    var pickedCallEl = el.querySelector("[data-solo-picked-call]");
    var stepbErrorEl = el.querySelector("[data-solo-stepb-error]");
    var whyListEl = el.querySelector("[data-solo-why-list]");
    var changeCallBtn = el.querySelector("[data-solo-change-call]");
    var submitBtn = el.querySelector("[data-solo-submit]");
    var resultMarkEl = el.querySelector("[data-solo-result-mark]");
    var resultHeardEl = el.querySelector("[data-solo-result-heard]");
    var partLawEl = el.querySelector("[data-solo-part-law]");
    var feedbackEl = el.querySelector("[data-solo-feedback]");
    var citeEl = el.querySelector("[data-solo-cite]");
    var retryBtn = el.querySelector("[data-solo-retry]");
    var lawLink = el.querySelector("[data-solo-law]");

    var generation = 0;
    var lastBody = null;
    var currentState = "idle";
    var remaining = 0;
    var callUi = initialCallState();
    var activeAttemptKey = null;
    var activeSlot = null;

    function showView(name) {
      Object.keys(views).forEach(function (k) {
        if (views[k]) views[k].hidden = (k !== name);
      });
    }

    function setCallError(msg) {
      var text = msg ? String(msg) : "";
      if (callErrorEl) {
        callErrorEl.textContent = text;
        callErrorEl.hidden = !text;
      }
      if (stepbErrorEl) {
        stepbErrorEl.textContent = text;
        stepbErrorEl.hidden = !text;
      }
    }

    function renderWhyOptions(leg) {
      if (!whyListEl) return;
      whyListEl.innerHTML = "";
      var opts = (leg && Array.isArray(leg.why_options)) ? leg.why_options : [];
      opts.forEach(function (opt) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn solo-why-chip" + (callUi.whyKey === opt.key ? " solo-why-chip--selected" : "");
        btn.setAttribute("role", "radio");
        btn.setAttribute("aria-checked", callUi.whyKey === opt.key ? "true" : "false");
        btn.setAttribute("data-solo-why-key", opt.key);
        btn.textContent = opt.label;
        btn.addEventListener("click", function () {
          callUi = stepTransition(callUi, { type: "pick_why", whyKey: opt.key });
          setCallError("");
          renderWhyOptions(leg);
          if (submitBtn) submitBtn.disabled = !canSubmitCall(callUi);
        });
        whyListEl.appendChild(btn);
      });
      if (submitBtn) submitBtn.disabled = !canSubmitCall(callUi);
    }

    function render(body) {
      if (!body || typeof body !== "object") return;
      lastBody = body;
      currentState = body.state || "idle";
      remaining = Math.max(0, Number(body.remaining_s) || 0);

      var attemptKey = body.attempt_id != null ? String(body.attempt_id) : String(body.started_at);
      if (attemptKey !== activeAttemptKey) {
        activeAttemptKey = attemptKey;
        activeSlot = null;
        callUi = initialCallState();
        setCallError("");
      }

      var mark = markFor(currentState);
      if (markEl) {
        markEl.textContent = mark.word;
        markEl.className = mark.cls;
      }
      if (clockEl) clockEl.textContent = clock(remaining);
      if (startBtn) startBtn.textContent = startLabel(currentState);
      if (evidenceEl) evidenceEl.textContent = huntEvidenceLine(body);

      if (Array.isArray(body.legs)) {
        body.legs.forEach(function (leg) {
          var tag = el.querySelector('[data-solo-leg-tag="' + leg.slot + '"]');
          if (tag) {
            tag.textContent = leg.called ? "CALLED" : (leg.passed ? "HEARD" : "HUNT");
          }
        });
      }

      if (currentState === "reception_passed") {
        var leg = activeCallLeg(body);
        if (leg) {
          if (activeSlot !== leg.slot) {
            activeSlot = leg.slot;
            callUi = initialCallState();
            setCallError("");
          }
          if (heardEl) heardEl.textContent = "Heard: " + (leg.evidence || "");
          if (legalPromptEl) legalPromptEl.textContent = leg.legal_prompt || "";
          if (pickedCallEl) {
            pickedCallEl.textContent = callUi.call ? ("Your call: " + callLabel(callUi.call) + " \u2014 pick why") : "";
          }
          renderWhyOptions(leg);
          showView(callUi.step === "b" ? "call-b" : "call-a");
          return;
        }
      }

      if (currentState === "passed" || currentState === "call_missed") {
        var resLeg = latestResultLeg(body);
        if (resultMarkEl) {
          resultMarkEl.textContent = mark.word;
          resultMarkEl.className = mark.cls;
        }
        if (resultHeardEl) {
          resultHeardEl.textContent = resLeg && resLeg.evidence ? resLeg.evidence : "";
        }
        if (partLawEl && resLeg && resLeg.call_result) {
          var cr = resLeg.call_result;
          if (cr.legal_passed) {
            partLawEl.textContent = "LEGAL CALL: RIGHT";
            partLawEl.className = "solo-part-mark solo-part-mark--ok";
          } else if (cr.call_correct && !cr.why_correct) {
            partLawEl.textContent = "CALL RIGHT \u00b7 WHY MISSED";
            partLawEl.className = "solo-part-mark solo-part-mark--miss";
          } else {
            partLawEl.textContent = "LEGAL CALL: MISSED";
            partLawEl.className = "solo-part-mark solo-part-mark--miss";
          }
        }
        if (feedbackEl) {
          feedbackEl.textContent = resLeg && resLeg.call_result ? (resLeg.call_result.feedback || "") : "";
        }
        if (citeEl) {
          citeEl.textContent = resLeg && resLeg.call_result && resLeg.call_result.cite
            ? ("Authority: " + resLeg.call_result.cite)
            : "";
        }
        if (lawLink && resLeg && resLeg.call_result && resLeg.call_result.law_section) {
          lawLink.setAttribute(
            "href",
            "/law?back=" + encodeURIComponent(backSelf) + "#" + encodeURIComponent(resLeg.call_result.law_section)
          );
        }
        showView("result");
        return;
      }

      showView("hunt");
    }

    function doStart() {
      generation += 1;
      var postGen = generation;
      callUi = initialCallState();
      setCallError("");
      fetch("/api/solos/" + encodeURIComponent(soloId) + "/start", { method: "POST" })
        .then(function (resp) {
          if (!resp) return null;
          return resp.json().catch(function () { return null; });
        })
        .then(function (body) {
          if (postGen !== generation) return;
          if (body) render(body);
        })
        .catch(function () { /* next poll refreshes */ });
    }

    if (startBtn) startBtn.addEventListener("click", doStart);
    if (retryBtn) retryBtn.addEventListener("click", doStart);

    var callChoiceBtns = el.querySelectorAll("[data-solo-call-choice]");
    for (var i = 0; i < callChoiceBtns.length; i += 1) {
      callChoiceBtns[i].addEventListener("click", function (ev) {
        var picked = ev.currentTarget.getAttribute("data-solo-call-choice");
        callUi = stepTransition(callUi, { type: "pick_call", call: picked });
        setCallError("");
        if (lastBody) render(lastBody);
      });
    }

    if (changeCallBtn) {
      changeCallBtn.addEventListener("click", function () {
        callUi = stepTransition(callUi, { type: "change_call" });
        setCallError("");
        if (lastBody) render(lastBody);
      });
    }

    if (submitBtn) {
      submitBtn.addEventListener("click", function () {
        if (!canSubmitCall(callUi) || !lastBody || activeSlot === null) return;
        generation += 1;
        var postGen = generation;
        submitBtn.disabled = true;
        fetch("/api/solos/" + encodeURIComponent(soloId) + "/call", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            attempt_id: lastBody.attempt_id,
            started_at: lastBody.started_at,
            band_slot: activeSlot,
            call: callUi.call,
            why_key: callUi.whyKey,
          }),
        })
          .then(function (resp) {
            return resp.json().catch(function () { return null; }).then(function (body) {
              return { status: resp.status, body: body };
            });
          })
          .then(function (res) {
            if (postGen !== generation) return;
            if (res.status >= 200 && res.status < 300 && res.body) {
              callUi = initialCallState();
              setCallError("");
              render(res.body);
            } else {
              var msg = (res.body && (res.body.reason || res.body.detail)) || "Could not submit legal call.";
              setCallError(msg);
              if (submitBtn) submitBtn.disabled = !canSubmitCall(callUi);
            }
          })
          .catch(function () {
            if (postGen !== generation) return;
            setCallError("Network error while submitting call.");
            if (submitBtn) submitBtn.disabled = !canSubmitCall(callUi);
          });
      });
    }

    function schedulePoll() {
      setTimeout(poll, 2000);
    }

    function poll() {
      var myGen = generation;
      fetch("/api/solos/" + encodeURIComponent(soloId))
        .then(function (resp) { return resp && resp.ok ? resp.json() : null; })
        .then(function (body) {
          if (myGen !== generation) return;
          if (body) render(body);
        })
        .catch(function () { /* keep last state */ })
        .finally(schedulePoll);
    }

    setInterval(function () {
      if (currentState !== "active") return;
      remaining = Math.max(0, remaining - 1);
      if (clockEl) clockEl.textContent = clock(remaining);
    }, 1000);

    // Expose deterministic state setter for headless render checks without mutating the console.
    window.__soloRenderApply = function (body, stepOverride) {
      if (stepOverride && stepOverride.step === "b") {
        callUi = { step: "b", call: stepOverride.call || "legal", whyKey: stepOverride.whyKey || null };
      } else {
        callUi = initialCallState();
      }
      activeAttemptKey = body && body.attempt_id != null
        ? String(body.attempt_id)
        : (body && body.started_at !== undefined ? String(body.started_at) : null);
      activeSlot = (body && Array.isArray(body.legs) && body.legs[0]) ? body.legs[0].slot : 0;
      render(body);
    };

    poll();
  }

  var screen = document.querySelector("[data-solo-screen]");
  if (screen) bindScreen(screen);

  var tiles = document.querySelectorAll("[data-solo-tile]");
  for (var j = 0; j < tiles.length; j += 1) {
    bindTile(tiles[j]);
  }
})();
