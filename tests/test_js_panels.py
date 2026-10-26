# ABOUTME: Runs the browser panels' own tests inside the Python suite, so one command covers both.
# ABOUTME: Uses node's built-in runner — the console gains no dependency to become testable.

import shutil
import subprocess
from pathlib import Path

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


def test_every_shipped_panel_has_its_own_test():
    """Each sigint101/static/*.js must have a matching tests/<name>.test.js.

    The at-least-one-test check below cannot catch a panel that ships with no test of its own —
    it passes as long as any panel has tests. This asserts per-panel coverage, so adding a panel
    without a test turns the suite red rather than leaving a silent gap.
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
    """
    node = shutil.which("node")
    assert node, ("node is not on PATH, so the panel tests cannot run. They are part of the "
                  "suite because sigint101/static/*.js is shipped code — install node, or say "
                  "plainly that the panels are going untested.")
    assert JS_TESTS, "no tests/*.test.js found — the panels would be shipping untested"

    result = subprocess.run([node, "--test", *[str(p) for p in JS_TESTS]],
                            cwd=REPO, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    # node --test exits 0 on zero tests, so a runner that silently discovered nothing would
    # otherwise read as a pass.
    assert "# fail 0" in result.stdout, result.stdout
    assert "# pass 0" not in result.stdout, "node discovered no tests: " + result.stdout
