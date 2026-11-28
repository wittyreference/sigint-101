// ABOUTME: Drives the Meteor-M LRPT rack panel: passive GET /api/meteor polling and Listen now POST.
// ABOUTME: Keeps past decoded pictures visible across refusals, lost contact, and empty runs.

(function () {
  "use strict";

  function formatMHz(hz) {
    if (typeof hz !== "number" || !isFinite(hz) || hz <= 0) return "";
    return (hz / 1000000).toFixed(3) + " MHz";
  }

  function formatElapsed(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0) return "";
    var whole = Math.floor(seconds);
    var mins = Math.floor(whole / 60);
    var secs = whole % 60;
    return mins + ":" + (secs < 10 ? "0" : "") + secs;
  }

  function formatUtc(epochSec) {
    if (typeof epochSec !== "number" || !isFinite(epochSec) || epochSec <= 0) return "";
    var d = new Date(epochSec * 1000);
    var iso = d.toISOString();
    return iso.slice(0, 10) + " " + iso.slice(11, 16) + " UTC";
  }

  function downBadge(body) {
    body = body || {};
    if (body.cause === "stopped" || body.state === "stopped") {
      return { badge: "STOPPED", cls: "decoder__status decoder__status--stopped" };
    }
    var badge = body.cause === "no_radio" ? "NO RADIO" :
      (body.cause === "missing_tool" ? "TOOL MISSING" : "UNAVAILABLE");
    return { badge: badge, cls: "decoder__status decoder__status--empty" };
  }

  function refusalView(body) {
    body = body || {};
    var down = downBadge(body);
    if (body.cause === "stopped" || body.state === "stopped") {
      var retry = typeof body.retry_in === "number" && body.retry_in > 0
        ? "retrying in " + Math.ceil(body.retry_in) + " s" : "retrying now";
      return {
        badge: down.badge,
        cls: down.cls,
        phase: "Stopped",
        note: (body.reason || "SatDump stopped") + " · " + retry,
      };
    }
    var msg = body.message || body.reason || "Receiver unavailable.";
    var note = body.next_step ? msg + " · " + body.next_step : msg;
    return {
      badge: down.badge,
      cls: down.cls,
      phase: "Cannot listen",
      note: note,
    };
  }

  function phaseView(body) {
    body = body || {};
    if (body.available === false || body.state === "stopped" || body.cause === "stopped") {
      return refusalView(body);
    }
    var state = body.state || "idle";
    var mhz = formatMHz(body.freq_hz);
    if (state === "starting") {
      return {
        badge: "STARTING",
        cls: "decoder__status",
        phase: mhz ? "Starting on " + mhz : "Starting receiver…",
        note: "",
      };
    }
    if (state === "searching") {
      return {
        badge: "LIVE",
        cls: "decoder__status decoder__status--live",
        phase: body.reason || (mhz ? "Listening on " + mhz + " · searching" : "Listening · searching"),
        note: "",
      };
    }
    if (state === "receiving") {
      var locked = body.lock === "locked";
      return {
        badge: "LIVE",
        cls: "decoder__status decoder__status--live",
        phase: locked
          ? (mhz ? "Locked on " + mhz + " · decoding frames" : "Locked · decoding frames")
          : (body.reason || (mhz ? "Listening on " + mhz + " · searching" : "Listening · searching")),
        note: "",
      };
    }
    if (state === "decoding") {
      var elapsed = formatElapsed(body.elapsed_s);
      return {
        badge: "LIVE",
        cls: "decoder__status decoder__status--live",
        phase: elapsed ? "Decoding pictures · " + elapsed : "Decoding pictures",
        note: "",
      };
    }
    if (state === "done") {
      return {
        badge: "DONE",
        cls: "decoder__status",
        phase: body.reason || "Pass saved",
        note: "",
      };
    }
    if (state === "no_picture") {
      return {
        badge: "NO PICTURE",
        cls: "decoder__status",
        phase: "No picture decoded",
        note: body.reason || "",
      };
    }
    if (state === "interrupted") {
      return {
        badge: "INTERRUPTED",
        cls: "decoder__status decoder__status--cached",
        phase: "Interrupted",
        note: body.reason || "",
      };
    }
    return {
      badge: "IDLE",
      cls: "decoder__status",
      phase: body.reason || "Ready to listen",
      note: "",
    };
  }

  function resolvePasses(body, previous) {
    var prev = Array.isArray(previous) ? previous : [];
    if (!body || typeof body !== "object") return prev;
    if (!Array.isArray(body.passes)) return prev;
    if (body.passes.length === 0 && prev.length > 0 && (
      body.available === false ||
      body.state === "no_picture" ||
      body.state === "interrupted" ||
      body.state === "stopped"
    )) {
      return prev;
    }
    return body.passes;
  }

  function passCaption(passRow, image) {
    passRow = passRow || {};
    var parts = [passRow.satellite || "Meteor (unidentified)"];
    var when = formatUtc(passRow.ended_at || passRow.started_at);
    if (when) parts.push(when);
    var mhz = formatMHz(passRow.freq_hz);
    if (mhz) parts.push(mhz);
    if (image && image.product) parts.push(String(image.product));
    if (passRow.ended_by === "handoff") parts.push("Cut short by radio handoff");
    return parts.join(" · ");
  }

  function formatRadio(radio) {
    if (!radio || typeof radio !== "object") return "Radio not recorded";
    if (radio.kind === "rtl-sdr") {
      if (radio.serial) return "RTL-SDR " + radio.serial;
      if (typeof radio.index === "number") return "RTL-SDR index " + radio.index;
      return "RTL-SDR";
    }
    return String(radio.kind || "Radio");
  }

  function passDetails(passRow) {
    passRow = passRow || {};
    return {
      radio: formatRadio(passRow.radio),
      mode: passRow.mode ? String(passRow.mode) : "",
      command: typeof passRow.command === "string" ? passRow.command : "",
    };
  }

  function imageSrc(image) {
    var p = (image && typeof image.path === "string") ? image.path : "";
    if (!p) return "";
    if (p.indexOf("data:image/") === 0 || p.indexOf("/media/") === 0) return p;
    return "/media/" + p;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      formatMHz: formatMHz,
      formatElapsed: formatElapsed,
      phaseView: phaseView,
      refusalView: refusalView,
      resolvePasses: resolvePasses,
      passCaption: passCaption,
      passDetails: passDetails,
      imageSrc: imageSrc,
    };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-meteor-panel]"));
  if (!panels.length) return;

  var ACTIVE_STATES = { starting: true, searching: true, receiving: true, decoding: true };
  var ACTIVE_MS = 1500;
  var IDLE_MS = 4000;
  var stopped = false;

  var panelState = panels.map(function (panel) {
    return { panel: panel, passes: [], signature: "", posting: false };
  });

  function renderGallery(entry, passes) {
    var sig = JSON.stringify(passes);
    if (sig === entry.signature) return;
    entry.signature = sig;
    var list = entry.panel.querySelector("[data-meteor-passes]");
    var empty = entry.panel.querySelector("[data-meteor-empty]");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    if (!passes.length) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    var doc = entry.panel.ownerDocument;
    passes.forEach(function (passRow) {
      var images = Array.isArray(passRow.images) ? passRow.images : [];
      if (!images.length) return;
      var li = doc.createElement("li");
      li.className = "meteor__pass";
      li.setAttribute("data-meteor-pass", String(passRow.id || ""));
      images.forEach(function (imgInfo) {
        var fig = doc.createElement("figure");
        fig.className = "meteor__figure";
        var img = doc.createElement("img");
        img.className = "gallery__img meteor__img";
        img.src = imageSrc(imgInfo);
        img.alt = passCaption(passRow, imgInfo);
        var cap = doc.createElement("figcaption");
        cap.className = "meteor__caption";
        cap.textContent = passCaption(passRow, imgInfo);
        fig.appendChild(img);
        fig.appendChild(cap);
        li.appendChild(fig);
      });
      var det = passDetails(passRow);
      var details = doc.createElement("details");
      details.className = "meteor__details";
      var summary = doc.createElement("summary");
      summary.className = "meteor__summary";
      summary.textContent = "Launch details";
      details.appendChild(summary);
      var meta = doc.createElement("p");
      meta.className = "meteor__meta";
      meta.textContent = det.mode ? det.radio + " · " + det.mode : det.radio;
      details.appendChild(meta);
      if (det.command) {
        var code = doc.createElement("code");
        code.className = "meteor__command";
        code.textContent = det.command;
        details.appendChild(code);
      }
      li.appendChild(details);
      list.appendChild(li);
    });
  }

  function applyBody(entry, body, ok) {
    var panel = entry.panel;
    var badge = panel.querySelector("[data-meteor-badge]");
    var phaseEl = panel.querySelector("[data-meteor-phase]");
    var noteEl = panel.querySelector("[data-meteor-note]");
    var btn = panel.querySelector("[data-meteor-listen]");

    var view = (!ok || !body || body.available === false)
      ? refusalView(body || { reason: "Lost contact with the console." })
      : phaseView(body);

    if (badge) {
      badge.textContent = view.badge;
      badge.className = view.cls;
    }
    if (phaseEl) phaseEl.textContent = view.phase;
    if (noteEl) {
      if (view.note) {
        noteEl.textContent = view.note;
        noteEl.hidden = false;
      } else {
        noteEl.textContent = "";
        noteEl.hidden = true;
      }
    }
    if (btn) {
      var active = Boolean(ok && body && body.available !== false && ACTIVE_STATES[body.state]);
      btn.disabled = entry.posting || active;
    }
    if (typeof DecoderPanel !== "undefined") {
      DecoderPanel.showCommand(panel, (ok && body && body.available !== false) ? body.command : null);
    }
    entry.passes = resolvePasses(body, entry.passes);
    renderGallery(entry, entry.passes);
  }

  function pollOnce() {
    return fetch("/api/meteor", { cache: "no-store" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        panelState.forEach(function (entry) { applyBody(entry, res.body, res.ok); });
        return Boolean(res.ok && res.body && ACTIVE_STATES[res.body.state]);
      })
      .catch(function () {
        if (stopped) return false;
        var lost = { available: false, reason: "Lost contact with the console." };
        panelState.forEach(function (entry) { applyBody(entry, lost, false); });
        return false;
      });
  }

  function tick() {
    pollOnce().then(function (active) {
      if (!stopped) setTimeout(tick, active ? ACTIVE_MS : IDLE_MS);
    });
  }

  panelState.forEach(function (entry) {
    var btn = entry.panel.querySelector("[data-meteor-listen]");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (entry.posting) return;
      entry.posting = true;
      btn.disabled = true;
      fetch("/api/meteor/listen", { method: "POST", cache: "no-store" })
        .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
        .then(function (res) {
          entry.posting = false;
          applyBody(entry, res.body, res.ok);
        })
        .catch(function () {
          entry.posting = false;
          applyBody(entry, { available: false, reason: "Lost contact with the console." }, false);
        });
    });
  });

  // Expose refresh hook for layout checks that stub fetch and inspect all states synchronously.
  window.__meteorRefresh = pollOnce;
  tick();
})();
