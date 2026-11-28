// ABOUTME: Tests the polkit rule granting only logind power-off actions to sigint101-power.
// ABOUTME: Evaluates deploy/system/50-sigint101-power.rules directly under Node with a fake polkit.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const RULE_PATH = path.resolve(__dirname, "..", "deploy", "system", "50-sigint101-power.rules");

function loadRule() {
  const source = fs.readFileSync(RULE_PATH, "utf8");
  const rules = [];
  const polkit = {
    Result: { YES: "yes", NO: "no", AUTH_ADMIN: "auth_admin", NOT_HANDLED: "not_handled" },
    addRule(fn) {
      rules.push(fn);
    },
  };
  vm.runInNewContext(source, { polkit }, { filename: RULE_PATH });
  assert.equal(rules.length, 1, "expected exactly one polkit.addRule registration");
  return { rule: rules[0], polkit };
}

const ALLOWED_ACTIONS = [
  "org.freedesktop.login1.power-off",
  "org.freedesktop.login1.power-off-multiple-sessions",
  "org.freedesktop.login1.power-off-ignore-inhibit",
];

test("allows only the three logind power-off actions for sigint101-power", () => {
  const { rule, polkit } = loadRule();
  for (const id of ALLOWED_ACTIONS) {
    assert.equal(rule({ id }, { user: "sigint101-power" }), polkit.Result.YES, id);
  }
});

test("returns undefined for other users even on power-off actions", () => {
  const { rule } = loadRule();
  for (const user of ["learner", "root", "pi", "sigint101", ""]) {
    for (const id of ALLOWED_ACTIONS) {
      assert.equal(rule({ id }, { user }), undefined, `${user} on ${id}`);
    }
  }
});

test("returns undefined for sigint101-power on any non-power-off action", () => {
  const { rule } = loadRule();
  for (const id of [
    "org.freedesktop.login1.reboot",
    "org.freedesktop.login1.reboot-multiple-sessions",
    "org.freedesktop.login1.suspend",
    "org.freedesktop.systemd1.manage-units",
    "org.freedesktop.policykit.exec",
  ]) {
    assert.equal(rule({ id }, { user: "sigint101-power" }), undefined, id);
  }
});
