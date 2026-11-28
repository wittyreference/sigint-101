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

  // The conversion from the browser's timeline to the console's clock, shared by every tab and
  // reload of this browser for one console boot. A page estimates it as its render time on the
  // console's clock less the browser time its response arrived. Any delay between the render and
  // the arrival makes that estimate lag, never lead, so the largest any page has seen is the
  // tightest, and it is kept in the browser's storage under the boot's epoch. Storage that is
  // missing, refused or full only costs the sharing: the page keeps its own estimate.
  var SHARED_KEY = "sigint101-clock-offset:";

  function readShared(storage, epoch) {
    try {
      var value = storage ? storage.getItem(SHARED_KEY + epoch) : null;
      var offset = value === null || value === undefined ? NaN : Number(value);
      return isFinite(offset) ? offset : null;
    } catch (err) {
      return null;
    }
  }

  function keepShared(storage, epoch, offset) {
    try {
      var shared = readShared(storage, epoch);
      if (storage && (shared === null || offset > shared)) {
        storage.setItem(SHARED_KEY + epoch, String(offset));
      }
    } catch (err) { /* this page keeps its own estimate */ }
  }

  // An action's stamp, on the console's clock: the browser time of the action plus the
  // conversion, the tightest one this browser has seen (read at each stamp, so a tab opened later
  // with a tighter estimate tightens this one too: later stamps only ever move later). A page's
  // own estimate is never later than the truth (its response arrived after the console rendered
  // it), so neither is the shared one. Without a receive time, the script's own start stands in.
  // No anchor, no stamp: the console then orders the request by its arrival. `origin` is the
  // page's navigation start on the browser's timeline, for stamping the act of opening it.
  function makeClock(anchor, now, received, storage, origin) {
    var start = typeof received === "number" && isFinite(received) ? received : now();
    var own = anchor ? anchor.ms - start : null;
    if (anchor) keepShared(storage, anchor.epoch, own);

    function at(browserTime) {
      var shared = readShared(storage, anchor.epoch);
      var offset = shared !== null && shared > own ? shared : own;
      return { epoch: anchor.epoch, at: Math.round((browserTime + offset) * 1000) / 1000 };
    }

    return {
      stamp: function () { return anchor ? at(now()) : null; },
      navigationStamp: function () {
        if (!anchor) return null;
        return typeof origin === "number" && isFinite(origin) ? at(origin) : at(now());
      },
    };
  }

  // The browser's storage shared by its tabs, or null where it is missing or refused.
  function sharedStorage() {
    try {
      return root.localStorage || null;
    } catch (err) {
      return null;
    }
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
  var clock = makeClock(anchor, function () { return browserNow(performance); },
                        receivedAt(performance), sharedStorage(), performance.timeOrigin);
  root.ActionClock = { stamp: clock.stamp, navigationStamp: clock.navigationStamp,
                       stamped: function (body) { return stamped(body, clock); } };
})(this);
