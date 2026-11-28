#!/bin/bash
# ABOUTME: Managed provisioning shim installed only on enrolled boxes by verified enrollment.
# ABOUTME: Contends on ROOT/locks/mutation.lock and delegates once to the enrolled dispatcher.
set -euo pipefail

UPDATER_ROOT="${SIGINT101_UPDATER_ROOT:-$HOME/.local/state/sigint101-updater}"
MUTATION_LOCK="$UPDATER_ROOT/locks/mutation.lock"
DISPATCHER="$UPDATER_ROOT/bin/dispatcher.py"
EXIT_GATE_BUSY=75

if [ ! -f "$UPDATER_ROOT/current.json" ] || [ ! -f "$UPDATER_ROOT/control.json" ]; then
  echo "provision-shim: updater root is not enrolled: $UPDATER_ROOT" >&2
  exit 1
fi

if [ ! -f "$MUTATION_LOCK" ] || [ -L "$MUTATION_LOCK" ]; then
  echo "provision-shim: missing or untrusted mutation lock: $MUTATION_LOCK" >&2
  exit 1
fi

exec 7<>"$MUTATION_LOCK"
if ! flock -x -n 7; then
  echo "provision-shim: busy — another update or provision holds mutation.lock" >&2
  exit "$EXIT_GATE_BUSY"
fi

if [ ! -x "$DISPATCHER" ]; then
  echo "provision-shim: enrolled dispatcher is missing or not executable: $DISPATCHER" >&2
  exit 1
fi

# Delegate once to the enrolled dispatcher, retaining the exclusive mutation gate on fd 7.
SIGINT101_MUTATION_GATE_FD=7 exec "$DISPATCHER" --root "$UPDATER_ROOT" "$@"
