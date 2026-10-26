# SIGINT 101 — STATUS

## NEXT ACTION
Lift the core lesson engine from elmer test-first (lessons loader with the intro/consolidation
stage split, progress store, event log, no-mock hardware probe), and install the right-sized
feature-factory harness.

## Where things stand (2026-10-26)
- Plan approved: rev 2, post adversarial review + educator pass (/home/mc/.claude/plans/eager-spinning-frost.md).
- P0 underway: repo scaffold committed (FastAPI layout, feature-factory config, test/lint tooling).
- Full scope, no cuts (D15); no artificial capability gates (D16); dev on the droplet,
  acceptance on spec5/pi5 over SSH (D14).

## Blockers
- Tailnet SSH from the droplet to spec5/pi5/nomad-b is closed (deferred); gates
  hardware-in-the-loop acceptance only, not droplet-side dev.

## Done / verified
- Scaffold committed and pushed to wittyreference/sigint-101 (public).
