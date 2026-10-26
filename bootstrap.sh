#!/usr/bin/env bash
# ABOUTME: Creates the venv, installs deps, and reports tool versions for SIGINT 101.
# ABOUTME: Safe to re-run; this is the one-command setup on the Nomad or any Linux host.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "bootstrap: deps installed"
# Re-arm the pre-commit git-identity guard. core.hooksPath is repo-local config and does
# not survive a clone, so every bootstrap re-points git at githooks/.
git config core.hooksPath githooks
echo "bootstrap: git hooks armed (core.hooksPath=githooks)"
.venv/bin/ruff --version
.venv/bin/pytest --version
