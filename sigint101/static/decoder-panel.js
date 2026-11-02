// ABOUTME: Shared by every decoder panel: the badge and line for a tool that stopped, and the strip
// ABOUTME: showing the exact command the console runs. A dead tool reads STOPPED, never LIVE.

(function (root) {
  "use strict";

  // How a panel reads when its poll answers "not available". A tool that died on its own is
  // STOPPED, with the tool's own last words and when the console will try it again; any other
  // refusal (no radio, a missing tool, the radio lent to another lesson) keeps the panel's wording.
  function downState(body, fallbackBadge, fallbackReason) {
    body = body || {};
    var reason = body.reason || fallbackReason;
    if (body.state === "stopped") {
      var retry = typeof body.retry_in === "number" && body.retry_in > 0
        ? "retrying in " + Math.ceil(body.retry_in) + " s" : "retrying now";
      return { badge: "STOPPED", cls: "decoder__status decoder__status--stopped",
               reason: reason + " · " + retry };
    }
    return { badge: fallbackBadge, cls: "decoder__status decoder__status--empty", reason: reason };
  }

  // The badge for an answering tool. A fresh one reads STARTING until it has held on a moment —
  // a tool retried on a busy dongle dies a few hundred ms in, and must not flash LIVE first.
  function liveBadge(body) {
    if (body && body.state === "starting") {
      return { text: "STARTING", cls: "decoder__status" };
    }
    return { text: "LIVE", cls: "decoder__status decoder__status--live" };
  }

  // The command line as the API sent it — a string read off the running process — or nothing.
  // Never rebuilt here: the strip shows only what the console reports it is running.
  function commandText(command) {
    return typeof command === "string" ? command : "";
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { downState: downState, liveBadge: liveBadge, commandText: commandText };
    return;
  }

  function showLive(badge, body) {
    if (!badge) return;
    var live = liveBadge(body);
    badge.textContent = live.text;
    badge.className = live.cls;
  }

  // Fill a panel's command strip, or hide it when nothing runs. The full line also rides in the
  // title so it survives truncation; a tap on the panel opens the strip to show it whole. In the
  // rack the strip sits under the panel rather than in its head, inside the [data-command-scope]
  // that holds both, and shows its whole line there; a lesson has no such scope, and its panel's
  // own strip is the one filled.
  function showCommand(panel, command) {
    var scope = panel.closest("[data-command-scope]") || panel;
    var strip = scope.querySelector("[data-command]");
    if (!strip) return;
    var text = commandText(command);
    var code = strip.querySelector("[data-command-text]");
    if (code && code.textContent !== text) code.textContent = text;
    strip.title = text;
    strip.hidden = !text;
    if (!text && strip.classList.contains("cmdstrip--open")) toggle(strip);
  }

  // Open the strip to its whole line over the panel, or close it back to one line.
  function toggle(strip) {
    var open = strip.classList.toggle("cmdstrip--open");
    strip.setAttribute("aria-expanded", open ? "true" : "false");
  }

  // Badge and empty line for a refused poll, through downState.
  function showDown(panel, badge, empty, body, fallbackBadge, fallbackReason) {
    var down = downState(body, fallbackBadge, fallbackReason);
    if (badge) { badge.textContent = down.badge; badge.className = down.cls; }
    if (empty) { empty.hidden = false; empty.textContent = down.reason; }
    showCommand(panel, null);
  }

  // A lesson's strip is a one-line preview in its panel's head. The touch target that opens it is
  // the whole panel (124 px by the full width), not the strip row alone, which is far under the
  // 72 px touch rule; the strip stays a button so a keyboard can open it too, and its click
  // reaches the panel's handler like any other tap there. Nothing running, nothing to open.
  function wire(strip) {
    var panel = strip.closest(".decoder") || strip;
    panel.addEventListener("click", function () {
      if (!strip.hidden) toggle(strip);
    });
  }

  // Only the lesson strips take a tap; the sweep's and the rack's show their whole line.
  Array.prototype.forEach.call(document.querySelectorAll("button[data-command]"), wire);

  root.DecoderPanel = { showCommand: showCommand, showDown: showDown, showLive: showLive,
                        downState: downState };
})(this);
