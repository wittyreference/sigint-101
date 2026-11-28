// ABOUTME: Tests the lesson page's tuning restore (lesson-tuning.js): one synchronous POST on load,
// ABOUTME: made before the panel scripts run, that a refusal or a lost console never breaks.

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");

const SOURCE = readFileSync(require.resolve("../sigint101/static/lesson-tuning.js"), "utf8");

// Runs the script as the browser would, with its own <script> tag and a recording XHR.
function load(lessonId, { sendThrows = false } = {}) {
  const requests = [];
  class FakeXHR {
    open(method, url, async) { this.request = { method, url, async }; }
    send(body) {
      this.request.body = body;
      requests.push(this.request);
      if (sendThrows) throw new Error("NetworkError: blocked");
    }
  }
  const script = { getAttribute: name => (name === "data-lesson-id" ? lessonId : null) };
  runInNewContext(SOURCE, { document: { currentScript: script }, XMLHttpRequest: FakeXHR });
  return requests;
}

test("a lesson page posts its tuning once, synchronously, as it loads", () => {
  const requests = load("7");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].method, "POST");
  assert.equal(requests[0].url, "/api/tuning?lesson_id=7");
  assert.equal(requests[0].async, false, "the panels' first polls must find the receivers home");
});

test("a refused or blocked post is swallowed, so the panels still load", () => {
  assert.equal(load("3", { sendThrows: true }).length, 1);
});

test("a page that names no lesson posts nothing", () => {
  assert.deepEqual(load(null), []);
});
