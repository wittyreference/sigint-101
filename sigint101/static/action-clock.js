// ABOUTME: Stamps each learner action (a lesson's tuning restore, a tune) on the console's own clock,
// ABOUTME: so the console orders late requests by when the learner acted, not when they arrived.

(function (root) {
  "use strict";

  // The page's <meta name="sigint101-clock" content="<epoch> <ms>">: the console's clock epoch
  // (its boot) and its monotonic time, in ms, when it rendered the page. Reading it writes nothing.
  function parseAnchor(content) {
    var parts = typeof content === "string" ? content.trim().split(/\s+/) : [];
    if (parts.length !== 2 || !parts[0]) return null;
    var ms = Number(parts[1]);
    return isFinite(ms) ? { epoch: parts[0], ms: ms } : null;
  }

  // The browser's shared timeline: performance.timeOrigin + performance.now(), the same for
  // every tab and reload in this browser, whatever each page's script was doing.
  function browserNow(perf) {
    return perf.timeOrigin + perf.now();
  }

  // When this page's response began to arrive, on that same timeline: the navigation's
  // responseStart, which the browser records as the bytes land, not when the script runs.
  // Null when the browser does not say.
  function receivedAt(perf) {
    var entries = perf.getEntriesByType ? perf.getEntriesByType("navigation") : [];
    var nav = entries && entries[0];
    return nav && nav.responseStart > 0 ? perf.timeOrigin + nav.responseStart : null;
  }

  // An action's stamp, on the console's clock: its time at render, plus the browser time since
  // the response arrived. The console rendered the page just before it arrived, so every tab maps
  // the shared timeline onto the console's clock the same way, off only by the network's delivery
  // time (well under a millisecond on the kiosk), never by a slow script. That only ever makes an
  // action look a little older. Without a receive time, the script's own start is used instead.
  // No anchor, no stamp: the console then orders the request by its arrival.
  function makeClock(anchor, now, received) {
    var start = typeof received === "number" && isFinite(received) ? received : now();
    return {
      stamp: function () {
        if (!anchor) return null;
        return { epoch: anchor.epoch, at: Math.round((anchor.ms + (now() - start)) * 1000) / 1000 };
      },
    };
  }

  // A request body with the stamp added beside its own fields.
  function stamped(body, clock) {
    var out = {};
    Object.keys(body).forEach(function (key) { out[key] = body[key]; });
    var stamp = clock && clock.stamp();
    if (stamp) { out.epoch = stamp.epoch; out.at = stamp.at; }
    return out;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseAnchor: parseAnchor, browserNow: browserNow, receivedAt: receivedAt,
                       makeClock: makeClock, stamped: stamped };
    return;
  }

  var meta = document.querySelector('meta[name="sigint101-clock"]');
  var clock = makeClock(parseAnchor(meta && meta.getAttribute("content")),
                        function () { return browserNow(performance); }, receivedAt(performance));
  root.ActionClock = { stamp: clock.stamp,
                       stamped: function (body) { return stamped(body, clock); } };
})(this);
