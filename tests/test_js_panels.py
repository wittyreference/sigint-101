# ABOUTME: Runs the browser panels' own tests inside the Python suite, so one command covers both.
# ABOUTME: Uses node's built-in runner — the console gains no dependency to become testable.

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# node --test discovers by filename, and this is the pattern the pre-write pipeline gate also
# looks for when deciding whether a new .js file has tests. Keeping them the same means a new
# panel cannot satisfy the gate with a file the runner would never execute.
JS_TESTS = sorted(REPO.glob("tests/*.test.js"))

# Every shipped panel is real code the kiosk runs, so each one must carry its own test file.
# Pairing them by name (sigint101/static/<panel>.js -> tests/<panel>.test.js) is what stops a new
# panel from shipping untested while the suite still reports green because some OTHER panel's
# test ran.
PANELS = sorted(REPO.glob("sigint101/static/*.js"))

# The totals node's TAP reporter ends a run with, one to a line: "# pass 127", "# fail 0". They
# are read as numbers, so the verdict does not rest on how a report happens to be worded.
TAP_TOTAL = re.compile(r"^# (pass|fail) (\d+)$", re.MULTILINE)


def test_every_shipped_panel_has_its_own_test():
    """Each sigint101/static/*.js must have a matching tests/<name>.test.js.

    The check below runs the test files that exist, so it cannot see a panel that ships with no
    test file at all. This asserts per-panel coverage, so adding a panel without a test turns the
    suite red rather than leaving a silent gap.
    """
    missing = [p.name for p in PANELS
               if not (REPO / "tests" / (p.stem + ".test.js")).exists()]
    assert not missing, ("these shipped panels have no tests/<name>.test.js and would go "
                         f"untested: {missing}")


def test_the_panel_javascript_has_tests_and_they_run():
    """Run every tests/*.test.js under node, and fail loudly if node is not here.

    Deliberately not a skip. A skipped suite reports success while testing nothing, which is the
    documented-step-that-cannot-run problem this repo has removed elsewhere. The panels are real
    shipped code; if their tests cannot run, that is a finding and not a shrug.

    Each file must pass on its own: node exits 0, no test fails, and at least one test the file
    itself defines passes. A file that tests nothing (empty, a typo that skips its tests, only
    skipped tests) is a failure, even when the other files pass.
    """
    node = shutil.which("node")
    assert node, ("node is not on PATH, so the panel tests cannot run. They are part of the "
                  "suite because sigint101/static/*.js is shipped code — install node, or say "
                  "plainly that the panels are going untested.")
    assert JS_TESTS, "no tests/*.test.js found — the panels would be shipping untested"

    problems = []
    # One run per file, because a run's totals cover all its files: run together, one file's
    # passing tests would cover for another file that tests nothing.
    for path in JS_TESTS:
        # The reporter is named, because node's default is not one format. Up to node 22 a piped
        # run prints TAP; from node 23 it prints the spec report written for people ("ℹ fail 0"),
        # and a check that reads whatever the default is then fails with every panel test passing.
        result = subprocess.run([node, "--test", "--test-reporter=tap", str(path)],
                                cwd=REPO, capture_output=True, text=True, timeout=120)
        report = result.stdout + result.stderr
        totals = {name: int(count) for name, count in TAP_TOTAL.findall(result.stdout)}
        # A file that defines no test is reported as ONE PASSING TEST named after the file itself
        # ("ok 1 - <path>", "# pass 1", exit 0; node 26.13). That stand-in is not a test of the
        # file's, so it is taken off the pass count. node's TAP escapes "\" and "#" in a name.
        tap_name = str(path).replace("\\", "\\\\").replace("#", "\\#")
        stand_in = re.search(rf"^ok \d+ - {re.escape(tap_name)}$", result.stdout, re.MULTILINE)
        own_passes = totals.get("pass", 0) - (1 if stand_in else 0)
        if result.returncode != 0:
            problems.append(f"{path.name}: node exited {result.returncode}\n{report}")
        elif not {"pass", "fail"} <= totals.keys():
            problems.append(f"{path.name}: node printed no TAP totals\n{report}")
        elif totals["fail"] != 0:
            problems.append(f"{path.name}: {totals['fail']} test(s) failed\n{report}")
        elif own_passes < 1:
            problems.append(f"{path.name}: no test of its own ran and passed\n{report}")
    assert not problems, "\n".join(problems)


# The check above, run on throwaway test files instead of the repo's own. Each file is written to
# tmp_path, never to tests/, and the check is pointed at it through JS_TESTS. A bad file is tried
# alone and after a file whose test passes, because in a run of all the files together a good
# file would cover for a bad one.
PASSING_JS = 'const test = require("node:test");\ntest("passes", () => {});\n'
BAD_JS = {
    "empty": "",
    "throws_before_any_test": ('throw new Error("broken before any test registered");\n'
                               'const test = require("node:test");\n'
                               'test("never registered", () => {});\n'),
    "missing_module": ('const panel = require("./no-such-panel.js");\n'
                       'const test = require("node:test");\n'
                       'test("never registered", () => {});\n'),
    "zero_tests": ('const test = require("node:test");\n'
                   'const enabled = false;\n'
                   'if (enabled) test("a typo leaves this unregistered", () => {});\n'),
    "empty_suite": ('const { describe } = require("node:test");\n'
                    'describe("a suite with nothing in it", () => {});\n'),
    "skipped_only": ('const test = require("node:test");\n'
                     'test("not run", { skip: true }, () => {});\n'),
    "todo_only": ('const test = require("node:test");\n'
                  'test("not run either", { todo: true }, () => {});\n'),
    "one_failing": ('const test = require("node:test");\n'
                    'const assert = require("node:assert");\n'
                    'test("fails", () => { assert.strictEqual(1, 2); });\n'),
    "throws_after_its_test_passed": ('const test = require("node:test");\n'
                                     'test("passes", () => {});\n'
                                     'setTimeout(() => { throw new Error("late"); }, 10);\n'),
}


def _js_check_on(monkeypatch, files):
    """Run the panel check on the given test files; return its failure message, or None."""
    monkeypatch.setitem(globals(), "JS_TESTS", files)
    try:
        test_the_panel_javascript_has_tests_and_they_run()
    except AssertionError as failure:
        return str(failure)
    return None


def _write_js(directory, name, source):
    path = directory / name
    path.write_text(source)
    return path


@pytest.mark.parametrize("kind", sorted(BAD_JS))
@pytest.mark.parametrize("beside_a_passing_file", [False, True], ids=["alone", "with_good"])
def test_a_js_test_file_that_runs_no_passing_test_of_its_own_fails_the_check(
        monkeypatch, tmp_path, kind, beside_a_passing_file):
    bad = _write_js(tmp_path, f"{kind}.test.js", BAD_JS[kind])
    files = [bad]
    if beside_a_passing_file:
        files = [_write_js(tmp_path, "good.test.js", PASSING_JS), bad]
    message = _js_check_on(monkeypatch, files)
    assert message is not None, f"{kind}.test.js was accepted as a pass"
    assert bad.name in message, f"the failure does not name {bad.name}: {message}"


def test_an_empty_js_test_file_whose_name_has_a_hash_fails_the_check(monkeypatch, tmp_path):
    # node's TAP report escapes "#" in a name, so the file's name must be matched as escaped.
    bad = _write_js(tmp_path, "odd#name.test.js", "")
    message = _js_check_on(monkeypatch, [_write_js(tmp_path, "good.test.js", PASSING_JS), bad])
    assert message is not None, "odd#name.test.js was accepted as a pass"


@pytest.mark.parametrize("sources", [
    [PASSING_JS],
    [PASSING_JS, PASSING_JS],
    [PASSING_JS + 'test("later", { skip: true }, () => {});\n'],
], ids=["one_passing", "two_passing", "passing_and_skipped"])
def test_js_test_files_that_each_pass_a_test_of_their_own_pass_the_check(
        monkeypatch, tmp_path, sources):
    files = [_write_js(tmp_path, f"f{i}.test.js", s) for i, s in enumerate(sources)]
    assert _js_check_on(monkeypatch, files) is None


@pytest.mark.parametrize("totals, status", [
    ("# tests 1\\n# pass 1\\n# fail 0\\n", 1),
    ("# tests 2\\n# pass 1\\n# fail 1\\n", 0),
    ("", 0),
], ids=["clean_totals_exit_1", "a_failure_exit_0", "no_totals_exit_0"])
def test_a_node_report_that_is_not_a_clean_pass_fails_the_check(
        monkeypatch, tmp_path, totals, status):
    # Real node never pairs these totals with that exit status, so a stand-in node on PATH does:
    # it reports one passing test, then the given totals, and exits with the given status. The
    # exit status, the fail count and the totals must each decide on their own.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "node"
    fake.write_text('#!/bin/sh\n'
                    'for a in "$@"; do last="$a"; done\n'
                    'printf "TAP version 13\\n# running %s\\nok 1 - passes\\n1..1\\n" "$last"\n'
                    f'printf "{totals}"\n'
                    f'exit {status}\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    good = _write_js(tmp_path, "good.test.js", PASSING_JS)
    message = _js_check_on(monkeypatch, [good])
    assert message is not None, "a node report that is not a clean pass was accepted"
    assert good.name in message, f"the failure does not name {good.name}: {message}"
