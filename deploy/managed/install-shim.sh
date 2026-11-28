#!/bin/bash
# ABOUTME: Managed install guard shim installed only on enrolled boxes by verified enrollment.
# ABOUTME: Refuses fresh legacy installation whenever an enrolled updater root is present.
set -euo pipefail

UPDATER_ROOT="${SIGINT101_UPDATER_ROOT:-$HOME/.local/state/sigint101-updater}"

if [ -f "$UPDATER_ROOT/current.json" ] || [ -f "$UPDATER_ROOT/control.json" ]; then
  echo "install-shim: this host is already enrolled in the managed updater; fresh legacy install is refused" >&2
  exit 1
fi

echo "install-shim: missing enrollment state under $UPDATER_ROOT; rerun verified enrollment" >&2
exit 1
