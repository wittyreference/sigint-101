// ABOUTME: Tests the lesson-stage navigation rules — where a Next/Previous step lands and how each
// ABOUTME: rail pip reads, and whether the completion button advances or offers a retry.

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const nav = require("../sigint101/static/lesson-nav.js");

test("Next and Previous step one panel at a time", () => {
  assert.equal(nav.stepTo(2, 1, 6), 3);
  assert.equal(nav.stepTo(2, -1, 6), 1);
});

test("a step off either end lands nowhere", () => {
  assert.equal(nav.stepTo(0, -1, 6), -1);   // no Previous before the first stage
  assert.equal(nav.stepTo(5, 1, 6), -1);    // Next on the last stage completes instead
  assert.equal(nav.stepTo(0, 1, 0), -1);    // nothing rendered
});

test("the open stage reads active", () => {
  assert.equal(nav.pipState(2, 2, 4, false), "active");
});

test("every stage reached before, behind or ahead of the open one, reads done", () => {
  assert.equal(nav.pipState(0, 2, 4, false), "done");   // went back past it
  assert.equal(nav.pipState(4, 2, 4, false), "done");   // reached, then stepped back from
});

test("a stage not reached yet waits", () => {
  assert.equal(nav.pipState(5, 2, 4, false), "ahead");
});

test("a sealed stage stays locked whatever the walk says", () => {
  // Before the commit the sealed stages are not rendered at all; nothing about the walk opens them.
  assert.equal(nav.pipState(4, 3, 5, true), "locked");
  assert.equal(nav.pipState(4, 4, 5, true), "locked");
});

test("Next and Prev update the progress line and leave sealed steps locked", () => {
  const stages = ["frame", "demo", "predict", "observe"];
  const states = ["active", "ahead", "ahead", "locked"];
  const pips = stages.map((name, i) => {
    const classes = new Set(["rail-pip--" + states[i]]);
    const attributes = new Map([["data-stage", name]]);
    return {
      tagName: "LI",
      getAttribute: key => attributes.get(key),
      setAttribute: (key, value) => attributes.set(key, value),
      removeAttribute: key => attributes.delete(key),
      classList: {
        contains: key => classes.has(key),
        toggle: (key, on) => on ? classes.add(key) : classes.delete(key),
      },
    };
  });
  const panels = stages.slice(0, -1).map((name, i) => ({
    hidden: i !== 0,
    getAttribute: key => key === "data-stage" ? name : null,
  }));
  const move = {};
  const navButton = direction => ({ addEventListener: (_, handler) => { move[direction] = handler; } });
  const saved = [];
  runInNewContext(readFileSync(require.resolve("../sigint101/static/lesson-nav.js"), "utf8"), {
    document: {
      querySelector: () => ({ getAttribute: key => key === "data-lesson-id" ? "1" : "0" }),
      querySelectorAll: selector => ({
        ".stage-panel": panels,
        ".rail-pip": pips,
        "[data-nav='next']": [navButton("next")],
        "[data-nav='prev']": [navButton("prev")],
      }[selector] || []),
    },
    fetch: (_url, options) => { saved.push(options.body); return Promise.resolve(); },
  });
  function check(current, expected) {
    assert.equal(panels.findIndex(panel => !panel.hidden), current);
    pips.forEach((pip, i) => {
      const visible = ["active", "done", "ahead", "locked"]
        .filter(state => pip.classList.contains("rail-pip--" + state));
      assert.deepEqual(visible, [expected[i]], stages[i]);
      assert.equal(pip.getAttribute("aria-current"), i === current ? "step" : undefined);
    });
  }
  move.next();
  check(1, ["done", "active", "ahead", "locked"]);
  move.prev();
  check(0, ["active", "done", "ahead", "locked"]);
  assert.deepEqual(saved, ["stage=demo", "stage=frame"]);
});

for (const [failure, failSave] of [
  ["an HTTP refusal", () => Promise.resolve({ ok: false, status: 400 })],
  ["a network error", () => Promise.reject(new Error("lost connection"))],
]) {
  test("Complete stays and can retry after " + failure, async () => {
    const status = { textContent: "" };
    let click;
    const button = {
      textContent: "Complete lesson",
      parentElement: { querySelector: s => s === "[data-complete-status]" ? status : null },
      getAttribute: name => name === "data-next-href" ? "/lesson/2" : null,
      addEventListener: (event, handler) => { if (event === "click") click = handler; },
    };
    const stage = { getAttribute: name => name === "data-lesson-id" ? "1" : null };
    const doc = {
      querySelector: s => ({ ".stage": stage, "[data-complete]": button }[s] || null),
      querySelectorAll: s => s === "[data-complete]" ? [button] : [],
    };
    const page = { location: "/lesson/1" };
    const requests = [];
    runInNewContext(readFileSync(require.resolve("../sigint101/static/lesson-nav.js"), "utf8"), {
      document: doc,
      window: page,
      fetch: (url, options) => {
        requests.push([url, options.method]);
        return requests.length === 1 ? failSave() : Promise.resolve({ ok: true, status: 200 });
      },
    });

    click({ currentTarget: button });
    await new Promise(setImmediate);
    assert.equal(page.location, "/lesson/1");
    assert.equal(status.textContent, "Progress couldn't be saved");
    assert.equal(button.textContent, "Retry save");
    assert.equal(button.disabled, false);

    click({ currentTarget: button });
    await new Promise(setImmediate);
    assert.equal(page.location, "/lesson/2");
    assert.deepEqual(requests, [
      ["/api/complete?lesson_id=1", "POST"],
      ["/api/complete?lesson_id=1", "POST"],
    ]);
  });
}

test("Commit follows radio selection and a submit without a choice is blocked except Skip", () => {
  const handlers = {};
  const submit = { disabled: true };
  const status = { textContent: "" };
  submit.parentElement = { querySelector: () => status };
  const radio = { checked: true };
  const form = {
    elements: [submit],
    querySelector: () => radio.checked ? radio : null,
    addEventListener: (event, handler) => { handlers[event] = handler; },
  };
  submit.classList = { contains: name => name === "btn-commit" };
  runInNewContext(readFileSync(require.resolve("../sigint101/static/lesson-nav.js"), "utf8"), {
    document: {
      querySelectorAll: selector => selector === "form[data-choice-commit]" ? [form] : [],
      querySelector: () => null,
    },
    window: { addEventListener: () => {} },
  });
  assert.equal(submit.disabled, false, "a restored selection should be ready at startup");
  assert.equal(status.textContent, "Choice set. Commit to unlock.");

  radio.checked = false;
  handlers.change();
  assert.equal(submit.disabled, true);
  assert.equal(status.textContent, "Choose one, comrade");
  let prevented = 0;
  const attempt = submitter => handlers.submit({ submitter, preventDefault() { prevented++; } });
  attempt(submit);
  assert.equal(prevented, 1);
  attempt({ formNoValidate: true });
  assert.equal(prevented, 1, "Skip must work without a choice");

  radio.checked = true;
  handlers.change();
  attempt(submit);
  assert.equal(prevented, 1);
  assert.equal(submit.disabled, false);
});
