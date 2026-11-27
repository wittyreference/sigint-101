# ABOUTME: Checks the Makefile's parallel and serial coverage gates.
# ABOUTME: Uses a dry run to inspect the commands without starting a nested test suite.

import shlex
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("target, workers", [("selftest", ["-n", "auto"]),
                                              ("selftest-serial", [])])
def test_selftest_targets_use_the_same_coverage_gate(target, workers):
    result = subprocess.run(["make", "--no-print-directory", "-n", target], cwd=REPO,
                            capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    command = shlex.split(result.stdout)
    assert command == [".venv/bin/pytest", *workers, "--cov=sigint101", "--cov-fail-under=80", "-q"]
