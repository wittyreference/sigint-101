// ABOUTME: Binds the Capture screen to the confirmed Waterfall tune and finite run status.
// ABOUTME: Fences stale replies and sends only deliberate Record or Stop requests.
(() => {
  "use strict";
  const panel = document.querySelector("[data-capture]");
  if (!panel) return;

  const find = (slot) => panel.querySelector(`[data-capture-${slot}]`);
  const record = find("start");
  const stop = find("stop");
  const messages = {
    idle: "Ready to record",
    starting: "Opening the radio…",
    recording: "Recording…",
    saving: "Saving…",
    saved: "Capture saved",
    stopped: "Capture stopped",
    error: "Capture failed",
  };
  let status = JSON.parse(document.querySelector("#capture-status").textContent);
  let generation = 0;
  let pending = null;
  let polling = false;
  let refusal = null;

  function paint() {
    const busy = ["starting", "recording", "saving"].includes(status.state);
    const tune = busy ? status.tune : status.confirmed;
    find("tune").textContent = tune
      ? `${(tune.hz / 1000000).toFixed(6)} MHz · ${tune.gain == null ? "Auto" : tune.gain + " dB"}`
      : "Waterfall tune unavailable";
    const ready = status.confirmed?.hz >= 300000000 && status.confirmed?.hz <= 928000000;
    const message = pending === "start" ? "Requesting capture…"
      : pending === "stop" ? "Requesting stop…"
      : refusal || status.reason || (!ready && !busy ? "Tune the Waterfall to 300–928 MHz."
        : status.finishing ? "Finishing save…" : messages[status.state]);
    find("state").textContent = message;
    const saved = find("saved");
    saved.hidden = !status.last_saved;
    saved.textContent = status.last_saved ? "Last saved clip · private SigMF pair" : "";
    record.disabled = !!pending || busy || !ready;
    stop.disabled = pending === "stop" || status.finishing || (!busy && pending !== "start");
    stop.textContent = status.state === "saving" && !status.finishing ? "Stop and discard" : "Stop";
  }

  async function refresh() {
    if (pending || polling || document.hidden) return;
    const epoch = generation;
    polling = true;
    try {
      const response = await fetch("/api/capture", {signal: AbortSignal.timeout(2000)});
      if (!response.ok) throw new Error("Capture status unavailable");
      const body = await response.json();
      if (generation === epoch) {
        if (body.run_id !== status.run_id || body.state !== status.state) refusal = null;
        status = body;
        paint();
      }
    } catch (_) {
      if (generation === epoch && !refusal) {
        find("state").textContent = "Capture status unavailable";
      }
    } finally {
      polling = false;
    }
  }

  async function send(action) {
    const epoch = ++generation;
    const options = {method: "POST", signal: AbortSignal.timeout(20000)};
    if (action === "start") {
      options.headers = {"Content-Type": "application/json"};
      options.body = JSON.stringify(status.confirmed);
    }
    pending = action;
    refusal = null;
    paint();
    try {
      const response = await fetch(`/api/capture/${action}`, options);
      const body = await response.json();
      if (generation !== epoch) return;
      status = body;
      if (!response.ok) refusal = body.reason || "Capture request refused";
    } catch (_) {
      if (generation === epoch) refusal = "Capture request did not return a result";
    } finally {
      if (generation === epoch) {
        pending = null;
        paint();
      }
    }
  }

  record.addEventListener("click", () => send("start"));
  stop.addEventListener("click", () => send("stop"));
  window.addEventListener("pageshow", () => refresh());
  paint();
  setInterval(refresh, 500);
})();
