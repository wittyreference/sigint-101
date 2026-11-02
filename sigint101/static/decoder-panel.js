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

  // The command line as the API sent it — a string read off the running process — or nothing.
  // Never rebuilt here: the strip shows only what the console reports it is running.
  function commandText(command) {
    return typeof command === "string" ? command : "";
  }

  // Under node's test runner the file is loaded for its pure helpers and there is no document.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { downState: downState, commandText: commandText };
    return;
  }

  // Fill a panel's command strip, or hide it when nothing runs. The full line also rides in the
  // title so it survives truncation; a tap toggles the strip open to show it whole.
  function showCommand(panel, command) {
    var strip = panel.querySelector("[data-command]");
    if (!strip) return;
    var text = commandText(command);
    var code = strip.querySelector("[data-command-text]");
    if (code && code.textContent !== text) code.textContent = text;
    strip.title = text;
    strip.hidden = !text;
    if (!text) {
      strip.classList.remove("cmdstrip--open");
      strip.setAttribute("aria-expanded", "false");
    }
  }

  // Badge and empty line for a refused poll, through downState.
  function showDown(panel, badge, empty, body, fallbackBadge, fallbackReason) {
    var down = downState(body, fallbackBadge, fallbackReason);
    if (badge) { badge.textContent = down.badge; badge.className = down.cls; }
    if (empty) { empty.hidden = false; empty.textContent = down.reason; }
    showCommand(panel, null);
  }

  function wire(strip) {
    strip.addEventListener("click", function () {
      var open = strip.classList.toggle("cmdstrip--open");
      strip.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-command]"), wire);

  root.DecoderPanel = { showCommand: showCommand, showDown: showDown, downState: downState };
})(this);
