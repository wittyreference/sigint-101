// ABOUTME: Keeps the low-battery shutdown banner live — polls read-only /api/power every 2 s.
// ABOUTME: Hides cleanly whenever the notice is absent, stale, malformed, or unreachable.

(function () {
  "use strict";

  var POLL_MS = 2000;

  function isFiniteNumber(v) {
    return typeof v === "number" && Number.isFinite(v);
  }

  // Pure: what an /api/power answer draws on .power-notice.
  function powerNoticeView(data) {
    var hidden = { visible: false, failed: false, headline: "", detail: "" };
    if (!data || data.active !== true || typeof data.failed !== "boolean") {
      return hidden;
    }
    var detail = (isFiniteNumber(data.voltage) && isFiniteNumber(data.threshold_v))
      ? "Pack " + data.voltage.toFixed(2) + " V (limit " + data.threshold_v.toFixed(2) +
        " V) · Plug in charger now to cancel"
      : "Plug in charger now to cancel";

    if (data.failed === true) {
      if (!isFiniteNumber(data.retry_in_s)) return hidden;
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

    if (!isFiniteNumber(data.remaining_s)) return hidden;
    var rem = Math.max(0, Math.ceil(data.remaining_s));
    var when = rem === 0 ? "POWERING OFF NOW" : "POWERING OFF IN " + rem + " s";
    return {
      visible: true,
      failed: false,
      headline: "LOW BATTERY · " + when,
      detail: detail,
    };
  }

  // Under node's test runner the file is loaded for its pure helper and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { powerNoticeView: powerNoticeView };
    return;
  }

  var banner = document.querySelector("[data-power-notice]");
  if (!banner) return;

  function draw(view) {
    banner.hidden = !view.visible;
    banner.classList.toggle("power-notice--failed", !!view.failed);
    var headline = banner.querySelector("[data-power-headline]");
    if (headline) headline.textContent = view.headline;
    var detail = banner.querySelector("[data-power-detail]");
    if (detail) detail.textContent = view.detail;
  }

  function poll() {
    fetch("/api/power", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { draw(powerNoticeView(data)); })
      .catch(function () { draw(powerNoticeView(null)); });
  }

  document.addEventListener("sigint101:power-poll", poll);
  setInterval(poll, POLL_MS);
})();
