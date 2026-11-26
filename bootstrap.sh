#!/usr/bin/env bash
# ABOUTME: Creates the venv, installs deps, arms the git hooks and reports tool versions.
# ABOUTME: Safe to re-run; warns if make selftest's node or jq is missing. provision.sh runs it.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "bootstrap: deps installed"
# Re-arm the git-identity guards: core.hooksPath makes git run every hook in githooks/. It is
# repo-local config and does not survive a clone, so every bootstrap re-points git at githooks/.
git config core.hooksPath githooks
echo "bootstrap: git hooks armed (core.hooksPath=githooks)"
.venv/bin/ruff --version
.venv/bin/pytest --version
# make selftest runs the panels' JavaScript tests under node (tests/test_js_panels.py) and fails
# without it. The course itself never runs node, so a missing one is a warning, not a stop.
if ! command -v node >/dev/null; then
  echo "bootstrap: WARNING: node is not on PATH; make selftest needs it for the panel tests." >&2
  echo "bootstrap: install Node.js 20.11+ (21.2+ on the 21 line); the course runs without it." >&2
fi
# The git hooks armed above run on git and bash alone. jq is for the Claude Code safety hooks in
# .claude/hooks/, which read their JSON input with it: pre-write-validate.sh refuses every write
# without it, and make selftest's credential-check tests run that hook. The course never runs jq.
if ! command -v jq >/dev/null; then
  echo "bootstrap: WARNING: jq is missing; make selftest's credential tests fail without it." >&2
  echo "bootstrap: without jq, the Claude Code hooks in .claude/hooks/ block every write." >&2
  echo "bootstrap: install jq (on Debian, apt install jq); git hooks and the course need no jq." >&2
fi
