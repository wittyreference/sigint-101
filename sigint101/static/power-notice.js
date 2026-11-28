// ABOUTME: Keeps the low-battery shutdown banner live — polls read-only /api/power every 2 s.
// ABOUTME: Uses sequenced bounded polls, monotonic countdowns, and an unreadable-status fallback.

(function () {
  "use strict";

  var POLL_MS = 2000;
  var TICK_MS = 1000;
  var REQUEST_TIMEOUT_MS = 3500;
  var SNAPSHOT_LIFETIME_MS = 15000;
  var HIDDEN = { visible: false, failed: false, headline: "", detail: "" };

  function isFiniteNumber(v) {
    return typeof v === "number" && Number.isFinite(v);
  }

  // Pure: what an /api/power answer draws on .power-notice.
  function powerNoticeView(data) {
    if (!data || data.active !== true || typeof data.failed !== "boolean") {
      return HIDDEN;
    }
    var detail = (isFiniteNumber(data.voltage) && isFiniteNumber(data.threshold_v))
      ? "Pack " + data.voltage.toFixed(2) + " V (limit " + data.threshold_v.toFixed(2) +
        " V) · Plug in charger now to cancel"
      : "Plug in charger now to cancel";

    if (data.failed === true) {
      if (!isFiniteNumber(data.retry_in_s)) return HIDDEN;
      var retry = Math.max(0, Math.ceil(data.retry_in_s));
      var retryText = retry === 0 ? "RETRYING NOW" : "RETRYING IN " + retry + " s";
      return {
        visible: true,
        failed: true,
        headline: "LOW BATTERY · AUTOMATIC POWER-OFF FAILED · " + retryText +
                  " · SAVE AND SHUT DOWN NOW",
        detail: detail,
      };
    }

    if (!isFiniteNumber(data.remaining_s)) return HIDDEN;
    var rem = Math.max(0, Math.ceil(data.remaining_s));
    var when = rem === 0 ? "POWERING OFF NOW" : "POWERING OFF IN " + rem + " s";
    return {
      visible: true,
      failed: false,
      headline: "LOW BATTERY · " + when,
      detail: detail,
    };
  }

  function expiredView(snapshot) {
    if (snapshot.failed) {
      return {
        visible: true,
        failed: true,
        headline: "LOW BATTERY · AUTOMATIC POWER-OFF FAILED · STATUS UNREADABLE",
        detail: "Cannot read current power status · Save and shut down now",
      };
    }
    return {
      visible: true,
      failed: false,
      headline: "LOW BATTERY · POWER STATUS UNREADABLE",
      detail: "Cannot read current power status · Plug in charger now",
    };
  }

  function classifyPowerReply(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      return { kind: "malformed" };
    }
    if (data.active === false) {
      var keys = Object.keys(data);
      if (keys.length === 1 && keys[0] === "active") {
        return { kind: "inactive" };
      }
      return { kind: "malformed" };
    }
    if (data.active === true && powerNoticeView(data).visible) {
      return { kind: "active", data: data };
    }
    return { kind: "malformed" };
  }

  // Sequenced snapshot controller: seeds from structured server state, ignores superseded replies,
  // treats malformed replies as read failures, advances countdown from a monotonic clock while
  // fresh, and keeps the warning marked unreadable when live status cannot be confirmed.
  function createPowerNoticeController(opts) {
    var lifetimeMs = (opts && isFiniteNumber(opts.lifetimeMs) && opts.lifetimeMs > 0)
      ? opts.lifetimeMs
      : SNAPSHOT_LIFETIME_MS;
    var nextSeq = 0;
    var appliedSeq = 0;
    var snapshot = null;

    function buildSnapshot(data, nowMs, seeded) {
      return {
        failed: data.failed === true,
        baseSec: Math.max(0, Math.ceil(data.failed ? data.retry_in_s : data.remaining_s)),
        voltage: isFiniteNumber(data.voltage) ? data.voltage : null,
        threshold_v: isFiniteNumber(data.threshold_v) ? data.threshold_v : null,
        receivedAtMs: nowMs,
        seeded: !!seeded,
        unreadable: false,
      };
    }

    function renderAt(nowMs) {
      if (!snapshot) return HIDDEN;
      var elapsedMs = Math.max(0, nowMs - snapshot.receivedAtMs);
      if (snapshot.unreadable || elapsedMs > lifetimeMs) {
        return expiredView(snapshot);
      }
      var sec = Math.max(0, Math.ceil(snapshot.baseSec - elapsedMs / 1000));
      return powerNoticeView({
        active: true,
        failed: snapshot.failed,
        remaining_s: snapshot.failed ? undefined : sec,
        retry_in_s: snapshot.failed ? sec : undefined,
        voltage: snapshot.voltage,
        threshold_v: snapshot.threshold_v,
      });
    }

    function seed(data, nowMs) {
      var cls = classifyPowerReply(data);
      if (cls.kind === "active") {
        snapshot = buildSnapshot(cls.data, nowMs, true);
        return renderAt(nowMs);
      }
      snapshot = null;
      return HIDDEN;
    }

    function startPoll() {
      nextSeq += 1;
      return nextSeq;
    }

    function applyError(seq, nowMs) {
      if (seq < appliedSeq) return null;
      appliedSeq = seq;
      if (snapshot && snapshot.seeded) {
        snapshot.unreadable = true;
      }
      return renderAt(nowMs);
    }

    function applyReply(seq, data, nowMs) {
      if (seq < appliedSeq) return null;
      var cls = classifyPowerReply(data);
      if (cls.kind === "malformed") {
        return applyError(seq, nowMs);
      }
      appliedSeq = seq;
      if (cls.kind === "inactive") {
        snapshot = null;
        return HIDDEN;
      }
      snapshot = buildSnapshot(cls.data, nowMs, false);
      return renderAt(nowMs);
    }

    function hasActive() {
      return snapshot !== null;
    }

    return {
      seed: seed,
      startPoll: startPoll,
      applyReply: applyReply,
      applyError: applyError,
      renderAt: renderAt,
      hasActive: hasActive,
    };
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      powerNoticeView: powerNoticeView,
      classifyPowerReply: classifyPowerReply,
      createPowerNoticeController: createPowerNoticeController,
      REQUEST_TIMEOUT_MS: REQUEST_TIMEOUT_MS,
      SNAPSHOT_LIFETIME_MS: SNAPSHOT_LIFETIME_MS,
    };
    return;
  }

  var banner = document.querySelector("[data-power-notice]");
  if (!banner) return;

  function monoNow() {
    if (typeof performance !== "undefined" && typeof performance.now === "function") {
      return performance.now();
    }
    return Date.now();
  }

  var controller = createPowerNoticeController({ lifetimeMs: SNAPSHOT_LIFETIME_MS });
  var initialRaw = banner.getAttribute ? banner.getAttribute("data-power-initial") : null;
  if (initialRaw) {
    try {
      controller.seed(JSON.parse(initialRaw), monoNow());
    } catch (_e) {
      /* ignore malformed initial attribute; live poll will recover */
    }
  }

  function draw(view) {
    if (!view) return;
    banner.hidden = !view.visible;
    banner.classList.toggle("power-notice--failed", !!view.failed);
    var headline = banner.querySelector("[data-power-headline]");
    if (headline) headline.textContent = view.headline;
    var detail = banner.querySelector("[data-power-detail]");
    if (detail) detail.textContent = view.detail;
  }

  function poll() {
    var seq = controller.startPoll();
    var settled = false;
    var abortCtrl = typeof AbortController === "function" ? new AbortController() : null;
    var timer = setTimeout(function () {
      if (settled) return;
      settled = true;
      if (abortCtrl) {
        try { abortCtrl.abort(); } catch (_e) { /* ignore */ }
      }
      draw(controller.applyError(seq, monoNow()));
    }, REQUEST_TIMEOUT_MS);

    var fetchOpts = { cache: "no-store" };
    if (abortCtrl) fetchOpts.signal = abortCtrl.signal;

    fetch("/api/power", fetchOpts)
      .then(function (r) {
        if (!r || !r.ok) throw new Error("http");
        return r.json();
      })
      .then(function (data) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        draw(controller.applyReply(seq, data, monoNow()));
      })
      .catch(function () {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        draw(controller.applyError(seq, monoNow()));
      });
  }

  document.addEventListener("sigint101:power-poll", poll);
  setInterval(poll, POLL_MS);
  setInterval(function () {
    if (controller.hasActive()) {
      draw(controller.renderAt(monoNow()));
    }
  }, TICK_MS);
})();
