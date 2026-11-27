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

# The panel tests use mock.timers.enable({ apis: ... }), which landed in Node 21.2 and was
# backported to 20.11. Checking the version up front avoids confusing failures on older nodes.
NODE_FLOOR_TEXT = "Node.js 20.11+ (21.2+ on the 21 line)"
NODE_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def _parse_node_version(raw: str) -> tuple[int, int, int] | None:
    match = NODE_VERSION_RE.match(raw.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _node_version_supported(version: tuple[int, int, int]) -> bool:
    major, minor, _ = version
    if major > 21:
        return True
    if major == 21:
        return minor >= 2
    if major == 20:
        return minor >= 11
    return False


def _check_node_version(node: str) -> None:
    result = subprocess.run([node, "--version"], cwd=REPO, capture_output=True, text=True,
                            timeout=15)
    raw = (result.stdout or result.stderr).strip()
    assert result.returncode == 0, (
        f"could not read node version (`{node} --version` exited {result.returncode}: "
        f"{raw or 'no output'}). The panel tests need {NODE_FLOOR_TEXT}."
    )
    version = _parse_node_version(raw)
    assert version is not None, (
        f"unreadable node version {raw!r} from `{node} --version`. "
        f"The panel tests need {NODE_FLOOR_TEXT}."
    )
    assert _node_version_supported(version), (
        f"node {raw} is too old for the panel tests; need {NODE_FLOOR_TEXT}."
    )


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
    """Run every tests/*.test.js under node, and fail loudly if node is missing or too old.

    Deliberately not a skip. A skipped suite reports success while testing nothing, which is the
    documented-step-that-cannot-run problem this repo has removed elsewhere. The panels are real
    shipped code; if their tests cannot run, that is a finding and not a shrug.

    Each file must pass on its own: node exits 0, no test fails, and at least one test the file
    itself defines passes. A file that tests nothing (empty, a typo that skips its tests, only
    skipped tests) is a failure, even when the other files pass.
    """
    node = shutil.which("node")
    assert node, ("node is not on PATH, so the panel tests cannot run. They are part of the "
                  "suite because sigint101/static/*.js is shipped code — install "
                  f"{NODE_FLOOR_TEXT}, or say plainly that the panels are going untested.")
    _check_node_version(node)
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
# alone, after a file whose test passes and before one, because in a run of all the files
# together a good file would cover for a bad one.
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
@pytest.mark.parametrize("order", ["alone", "good_first", "bad_first"])
def test_a_js_test_file_that_runs_no_passing_test_of_its_own_fails_the_check(
        monkeypatch, tmp_path, kind, order):
    # Both orders beside a good file, so a check that judges only the first or only the last
    # file cannot pass.
    bad = _write_js(tmp_path, f"{kind}.test.js", BAD_JS[kind])
    good = _write_js(tmp_path, "good.test.js", PASSING_JS)
    files = {"alone": [bad], "good_first": [good, bad], "bad_first": [bad, good]}[order]
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
                    'if [ "$1" = "--version" ] || [ "$1" = "-v" ]; then echo v22.0.0; exit 0; fi\n'
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


def _install_fake_node(monkeypatch, tmp_path, body):
    """Put a stand-in node script at the front of PATH and return its directory."""
    bin_dir = tmp_path / "node_bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "node"
    fake.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def test_missing_node_fails_with_a_clear_message_naming_the_floor(monkeypatch, tmp_path):
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    good = _write_js(tmp_path, "good.test.js", PASSING_JS)
    message = _js_check_on(monkeypatch, [good])
    assert message is not None, "missing node was accepted"
    assert "node" in message.lower() and "20.11" in message, message


@pytest.mark.parametrize("version", ["v18.19.0", "v20.10.0", "v20.10.9", "v21.0.0", "v21.1.9"])
def test_an_old_node_fails_before_running_tests_and_names_the_floor(
        monkeypatch, tmp_path, version):
    calls = tmp_path / "node.calls"
    _install_fake_node(
        monkeypatch,
        tmp_path,
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        f'if [ "$1" = "--version" ] || [ "$1" = "-v" ]; then echo "{version}"; exit 0; fi\n'
        'printf "TAP version 13\\nok 1 - passes\\n1..1\\n# tests 1\\n# pass 1\\n# fail 0\\n"\n'
        'exit 0',
    )
    good = _write_js(tmp_path, "good.test.js", PASSING_JS)
    message = _js_check_on(monkeypatch, [good])
    assert message is not None, f"old node {version} was accepted"
    assert version in message, f"failure does not name {version}: {message}"
    assert "20.11" in message, f"failure does not name the 20.11 floor: {message}"
    logged = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert "--test" not in logged, f"panel tests ran despite old node {version}: {logged}"


@pytest.mark.parametrize("version", ["v20.11.0", "v20.18.1", "v21.2.0", "v22.0.0", "v24.0.0-rc.1"])
def test_a_supported_node_version_passes_the_version_check(monkeypatch, tmp_path, version):
    _install_fake_node(
        monkeypatch,
        tmp_path,
        f'if [ "$1" = "--version" ] || [ "$1" = "-v" ]; then echo "{version}"; exit 0; fi\n'
        'printf "TAP version 13\\nok 1 - passes\\n1..1\\n# tests 1\\n# pass 1\\n# fail 0\\n"\n'
        'exit 0',
    )
    good = _write_js(tmp_path, "good.test.js", PASSING_JS)
    assert _js_check_on(monkeypatch, [good]) is None


@pytest.mark.parametrize("body", [
    'echo "not-a-version"; exit 0',
    'echo "version broken" >&2; exit 1',
], ids=["unreadable", "nonzero"])
def test_an_unreadable_or_failing_node_version_fails_naming_the_floor(
        monkeypatch, tmp_path, body):
    _install_fake_node(monkeypatch, tmp_path, body)
    good = _write_js(tmp_path, "good.test.js", PASSING_JS)
    message = _js_check_on(monkeypatch, [good])
    assert message is not None, "broken node --version was accepted"
    assert "20.11" in message, f"failure does not name the 20.11 floor: {message}"
