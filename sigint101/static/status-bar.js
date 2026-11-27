// ABOUTME: Refreshes the kiosk's radio state and last measured PPM from the hardware endpoint.
// ABOUTME: Adds an SNR readout only while the waterfall has a current measurement.

(function () {
  "use strict";

  var radio = document.querySelector("[data-status-radio]");
  var ppm = document.querySelector("[data-status-ppm]");
  var battery = document.querySelector(".readout--batt");
  var snr = document.querySelector(".readout--snr");

  function setSnr(value) {
    if (!Number.isFinite(value)) {
      if (snr) snr.remove();
      snr = null;
      return;
    }
    if (!snr) {
      snr = document.createElement("span");
      snr.className = "readout readout--snr readout__label";
      snr.title = "Live signal-to-noise ratio";
      battery.before(snr);
    }
    snr.textContent = "SNR " + value.toFixed(1) + " dB";
  }

  function draw(body) {
    var state = body && body.state;
    radio.textContent = state === "present" ? "RADIO"
      : state === "busy" ? "RADIO BUSY" : state === "absent" ? "NO RADIO" : "RADIO —";
    if (body) {
      var value = body.ppm;
      ppm.textContent = "PPM " + (Number.isFinite(value) ? (value >= 0 ? "+" : "") + value : "—");
    }
  }

  function poll() {
    fetch("/api/hardware", { cache: "no-store" })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(draw)
      .catch(function () { draw(null); })
      .then(function () { setTimeout(poll, 5000); });
  }

  window.StatusBar = { setSnr: setSnr };
  if (radio && ppm) poll();
})();
