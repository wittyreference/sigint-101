#!/usr/bin/env bash
# ABOUTME: Creates the venv, installs deps, and reports tool versions for SIGINT 101.
# ABOUTME: Safe to re-run; this is the one-command setup on the Nomad or any Linux host.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "bootstrap: deps installed"
.venv/bin/ruff --version
.venv/bin/pytest --version
