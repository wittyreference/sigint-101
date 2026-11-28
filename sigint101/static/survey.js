// ABOUTME: Lists read-only Wi-Fi channel census rows on the console by polling /api/survey (M39).
// ABOUTME: Renders strictly from envelope metadata fields — never payload — and refuses honestly on 503.

(function () {
  "use strict";

  var ENVELOPE_FIELDS = Object.freeze(["ssid", "bssid", "band", "channel", "freq_mhz", "signal", "security"]);
  var CENSUS_FIELDS = Object.freeze(["band", "channel", "count", "hidden", "open", "strongest", "ssids"]);

  // Pick strictly the allowlisted census fields; any unexpected or payload key is dropped here.
  function envelopeRow(raw) {
    raw = raw && typeof raw === "object" ? raw : {};
    return {
      band: typeof raw.band === "string" && raw.band ? raw.band : null,
      channel: typeof raw.channel === "number" ? raw.channel : null,
      count: typeof raw.count === "number" ? raw.count : 0,
      hidden: typeof raw.hidden === "number" ? raw.hidden : 0,
      open: typeof raw.open === "number" ? raw.open : 0,
      strongest: typeof raw.strongest === "number" ? raw.strongest : null,
      ssids: Array.isArray(raw.ssids) ? raw.ssids.map(String) : [],
    };
  }

  function channelLabel(row) {
    var clean = envelopeRow(row);
    var ch = clean.channel != null ? "CH " + clean.channel : "CH —";
    if (!clean.band) return ch;
    var shortBand = clean.band.replace(/\s*GHz$/i, "G");
    return shortBand + " " + ch;
  }

  function countLabel(row) {
    var clean = envelopeRow(row);
    return clean.count + (clean.count === 1 ? " AP" : " APs");
  }

  function signalLabel(row) {
    var clean = envelopeRow(row);
    return clean.strongest != null ? clean.strongest + "%" : "—";
  }

  function summary(row) {
    var clean = envelopeRow(row);
    var parts = [];
    if (clean.ssids.length) {
      var shown = clean.ssids.slice(0, 2).join(", ");
      if (clean.ssids.length > 2) shown += ", +" + (clean.ssids.length - 2);
      parts.push(shown);
    }
    if (clean.hidden > 0) parts.push(clean.hidden + " hidden");
    if (clean.open > 0) parts.push(clean.open + " open");
    return parts.length ? parts.join(" · ") : "(no SSID)";
  }

  function badgeState(body) {
    if (body && body.cached === true) {
      return {
        text: "CACHED",
        cls: "decoder__status decoder__status--cached",
      };
    }
    return { text: "LIVE", cls: "decoder__status decoder__status--live" };
  }

  function emptyMessage(body) {
    if (body && body.cached === true) {
      return "NetworkManager's cached list is empty";
    }
    return "no Wi-Fi networks heard in this scan";
  }

  function pollAnswer(status, body) {
    var ok = status >= 200 && status < 300;
    if (body && typeof body === "object") {
      if (body.available === false && typeof body.reason === "string" && body.reason) return body;
      if (ok && body.available === true && Array.isArray(body.channels)) return body;
    }
    return {
      available: false,
      command: null,
      channels: [],
      reason: status ? "the console could not run the Wi-Fi survey (HTTP " + status + ")"
                     : "Lost contact with the console.",
    };
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      ENVELOPE_FIELDS: ENVELOPE_FIELDS,
      CENSUS_FIELDS: CENSUS_FIELDS,
      envelopeRow: envelopeRow,
      channelLabel: channelLabel,
      countLabel: countLabel,
      signalLabel: signalLabel,
      summary: summary,
      badgeState: badgeState,
      emptyMessage: emptyMessage,
      pollAnswer: pollAnswer,
    };
    return;
  }

  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-survey-panel]"));
  if (!panels.length) return;

  var POLL_MS = 10000;     // polls NetworkManager's cached AP list (--rescan no); does not trigger a scan
  var RETRY_MS = 5000;     // when no Wi-Fi radio is ready, poll gently until one appears
  var stopped = false;

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(panel, body) {
    var list = panel.querySelector("[data-survey-list]");
    var empty = panel.querySelector("[data-survey-empty]");
    var badge = panel.querySelector("[data-survey-badge]");
    var rows = (body.channels || []).map(envelopeRow);
    list.innerHTML = "";
    rows.forEach(function (row) {
      var li = document.createElement("li");
      li.className = "decoder__item";
      li.innerHTML =
        '<span class="decoder__model">' + esc(channelLabel(row)) + "</span>" +
        '<span class="decoder__mod">' + esc(countLabel(row)) + "</span>" +
        '<span class="decoder__read">' + esc(summary(row)) + "</span>" +
        '<span class="decoder__tone">' + esc(signalLabel(row)) + "</span>";
      list.appendChild(li);
    });
    if (body && body.cached === true) {
      if (badge) {
        var state = badgeState(body);
        badge.textContent = state.text;
        badge.className = state.cls;
      }
    } else {
      DecoderPanel.showLive(badge, body);
    }
    DecoderPanel.showCommand(panel, body.command);
    if (empty) {
      empty.hidden = rows.length > 0;
      if (!rows.length) {
        empty.textContent = body && body.cached === true
          ? emptyMessage(body)
          : DecoderPanel.quietText(body, emptyMessage(body));
      }
    }
  }

  function showEmpty(panel, body) {
    var list = panel.querySelector("[data-survey-list]");
    var empty = panel.querySelector("[data-survey-empty]");
    var badge = panel.querySelector("[data-survey-badge]");
    if (list) list.innerHTML = "";
    DecoderPanel.showDown(panel, badge, empty, body);
  }

  function tick() {
    fetch("/api/survey", { cache: "no-store" })
      .then(function (r) {
        return r.json().then(function (b) { return pollAnswer(r.status, b); },
                             function () { return pollAnswer(r.status, null); });
      }, function () { return pollAnswer(null, null); })
      .then(function (body) {
        var waiting = body.available === false;
        panels.forEach(function (p) { if (waiting) showEmpty(p, body); else render(p, body); });
        if (!stopped) setTimeout(tick, waiting ? RETRY_MS : POLL_MS);
      })
      .catch(function () {
        if (!stopped) setTimeout(tick, RETRY_MS);
      });
  }

  tick();
})();
