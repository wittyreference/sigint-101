// ABOUTME: Keeps the status bar's battery gauge live — polls /api/battery every 30 s and redraws it.
// ABOUTME: No reading (or no answer) draws the dash and an empty gauge, never a remembered number.

(function () {
  "use strict";

  var POLL_MS = 30000;

  // Pure: what an /api/battery answer draws. Null (the console did not answer) and an absent
  // reading both draw "unknown", the same as the server-rendered bar does.
  function batteryView(data) {
    if (!data || !data.present || typeof data.percent !== "number") {
      return { text: "—", width: 0, low: false, charging: false, title: "Battery" };
    }
    return {
      text: data.percent + "%",
      width: data.percent,
      low: !!data.low,
      charging: !!data.charging,
      title: typeof data.voltage === "number" ? "Battery " + data.voltage.toFixed(2) + " V"
                                              : "Battery",
    };
  }

  // Under node's test runner the file is loaded for its pure helper and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { batteryView: batteryView };
    return;
  }

  var gauge = document.querySelector(".readout--batt");
  if (!gauge) return;

  function draw(view) {
    gauge.classList.toggle("batt--low", view.low);
    gauge.setAttribute("title", view.title);
    var fill = gauge.querySelector(".batt__fill");
    if (fill) fill.style.width = view.width + "%";
    var bolt = gauge.querySelector(".batt__bolt");
    if (bolt) bolt.hidden = !view.charging;
    var value = gauge.querySelector(".readout__value");
    if (value) value.textContent = view.text;
  }

  function poll() {
    fetch("/api/battery", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { draw(batteryView(data)); })
      .catch(function () { draw(batteryView(null)); });
  }

  setInterval(poll, POLL_MS);
})();
