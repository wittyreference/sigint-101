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

  // Whether this document came out of the browser's cache rather than from the console: Back or
  // Forward served from the HTTP cache, or a cache delivery. Its clock meta is from its first
  // render, however long ago, so it cannot be mapped; its requests are ordered by arrival. (The
  // console sends every page no-store, so this is the fallback, not the rule.)
  function fromCache(perf) {
    var entries = perf.getEntriesByType ? perf.getEntriesByType("navigation") : [];
    var nav = entries && entries[0];
    if (!nav) return false;
    if (nav.deliveryType === "cache") return true;
    return nav.type === "back_forward" && nav.transferSize === 0;
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
                       fromCache: fromCache, makeClock: makeClock, stamped: stamped };
    return;
  }

  var meta = document.querySelector('meta[name="sigint101-clock"]');
  var anchor = fromCache(performance) ? null : parseAnchor(meta && meta.getAttribute("content"));
  var clock = makeClock(anchor,
                        function () { return browserNow(performance); }, receivedAt(performance));
  root.ActionClock = { stamp: clock.stamp,
                       stamped: function (body) { return stamped(body, clock); } };
})(this);
