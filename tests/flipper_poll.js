// ABOUTME: Runs the Flipper panel's own poll (flipper.js over decoder-panel.js) against replies,
// ABOUTME: in a vm with a stand-in page built from the panel macro; reports what the panel shows.

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const REPO = path.resolve(__dirname, "..");
const STATIC = path.join(REPO, "sigint101", "static");
const PANELS_HTML = path.join(REPO, "sigint101", "templates", "_panels.html");

// The panel as the page first draws it, read from the flipper() macro, so the "before any answer"
// text is the template's own and not a copy that could drift from it.
function macroText(attr) {
  const html = fs.readFileSync(PANELS_HTML, "utf8");
  const macro = html.match(/\{% macro flipper\(\) %\}([\s\S]*?)\{% endmacro %\}/)[1];
  const tag = macro.match(new RegExp("<[^>]*\\b" + attr + "\\b[^>]*>([^<]*)<"));
  return {
    text: tag[1].replace(/&hellip;/g, "…").replace(/&middot;/g, "·"),
    hidden: /\shidden[\s>]/.test(tag[0]),
  };
}

function element(attr) {
  const start = macroText(attr);
  return { textContent: start.text, hidden: start.hidden, className: "", title: "" };
}

// One panel: just what flipper.js and decoder-panel.js reach for. No command strip in a lesson's
// Flipper panel head, so showCommand finds none and returns.
function standInPage() {
  const parts = {
    "[data-flipper-badge]": element("data-flipper-badge"),
    "[data-flipper-empty]": element("data-flipper-empty"),
    "[data-flipper-card]": element("data-flipper-card"),
    "[data-flipper-name]": element("data-flipper-name"),
    "[data-flipper-line]": element("data-flipper-line"),
    "[data-flipper-radio]": element("data-flipper-radio"),
  };
  const panel = {
    querySelector: (sel) => parts[sel] || null,
    closest: () => null,
  };
  const document = {
    querySelectorAll: (sel) => (sel === "[data-flipper-panel]" ? [panel] : []),
  };
  return { document, parts };
}

// The answer the console gives, as fetch hands it over. A Response's json() rejects on a body
// that is not JSON (a server fault's "Internal Server Error" page); status null is a request that
// got no answer at all, which fetch rejects.
function fetchAnswering(status, text) {
  return function () {
    if (status === null) return Promise.reject(new TypeError("Failed to fetch"));
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status: status,
      json: () => Promise.resolve().then(() => JSON.parse(text)),
    });
  };
}

// Load both scripts as the browser does, let each poll's promises settle, and read the panel.
async function showPoll(status, text, next, beforeNext) {
  const { document, parts } = standInPage();
  const timers = [];
  let nextPoll;
  const context = vm.createContext({
    document: document,
    fetch: fetchAnswering(status, text),
    setTimeout: (fn, ms) => { nextPoll = fn; return timers.push(ms); },
  });
  for (const name of ["decoder-panel.js", "flipper.js"]) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, name), "utf8"), context, { filename: name });
  }
  await new Promise((r) => setImmediate(r));
  if (next) {
    if (beforeNext) beforeNext(context.DecoderPanel);
    context.fetch = fetchAnswering(...next);
    nextPoll();
    await new Promise((r) => setImmediate(r));
  }
  const badge = parts["[data-flipper-badge]"];
  const empty = parts["[data-flipper-empty]"];
  return {
    badge: badge.textContent,
    badgeClass: badge.className,
    empty: empty.textContent,
    emptyHidden: empty.hidden,
    cardHidden: parts["[data-flipper-card]"].hidden,
    name: parts["[data-flipper-name]"].textContent,
    line: parts["[data-flipper-line]"].textContent,
    nextPollMs: timers,
  };
}

module.exports = { showPoll: showPoll, macroText: macroText };

// From Python: `node tests/flipper_poll.js` reads [status, text] as JSON on stdin and prints what
// the panel shows, so the route's real answer can be put through the panel's real script.
if (require.main === module) {
  const [status, text] = JSON.parse(fs.readFileSync(0, "utf8"));
  showPoll(status, text).then((shown) => process.stdout.write(JSON.stringify(shown)));
}
