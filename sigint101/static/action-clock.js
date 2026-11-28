// ABOUTME: Stamps each learner action (a lesson's tuning restore, a tune) on the console's own clock,
// ABOUTME: so the console orders late requests by when the learner acted, not when they arrived.

(function (root) {
  "use strict";

  // The page's <meta name="sigint101-clock" content="<epoch> <ms>">: the console's clock epoch
  // and its time, in ms, when it rendered the page. Reading it on the GET writes nothing.
  function parseAnchor(content) {
    var parts = typeof content === "string" ? content.trim().split(/\s+/) : [];
    if (parts.length !== 2 || !parts[0]) return null;
    var ms = Number(parts[1]);
    return isFinite(ms) ? { epoch: parts[0], ms: ms } : null;
  }

  // An action's stamp: the console's time at render plus the time the page has run since, on
  // the page's own monotonic clock. Every page and tab is anchored to the same console clock, so
  // their stamps compare; a reload takes a fresh anchor. The stamp runs a little behind the
  // console's own time (by the page's load time), which only ever makes an action look older.
  // No anchor, no stamp: the console then orders the request by its arrival.
  function makeClock(anchor, now) {
    var start = now();
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
    module.exports = { parseAnchor: parseAnchor, makeClock: makeClock, stamped: stamped };
    return;
  }

  var meta = document.querySelector('meta[name="sigint101-clock"]');
  var clock = makeClock(parseAnchor(meta && meta.getAttribute("content")),
                        function () { return performance.now(); });
  root.ActionClock = { stamp: clock.stamp,
                       stamped: function (body) { return stamped(body, clock); } };
})(this);
