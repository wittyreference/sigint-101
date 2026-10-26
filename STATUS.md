# SIGINT 101 — STATUS

## NEXT ACTION
Two owner decisions gate the next work: (1) unblock the tailnet SSH so HIL verification on spec5
can begin (nothing is hardware-verified yet); (2) answer the three shell-preview taste questions
so the lesson panels (gain/provenance/ppm) can be wired. Blocker-free work remaining: M5 (legal
card-sort), M9-M18 lesson content + their decode backends, counter-surveillance, exam trainer,
kiosk/provisioning/updater, the Flipper FAP.

## Where things stand (2026-10-26)
- Repo live + green: github.com/wittyreference/sigint-101 (main @ 8a3c739). `make selftest`
  252 tests, ~93% coverage, lint clean, pristine.
- Modules (11): events, progress, hardware (no-mock probe), lessons (intro/consolidation +
  commit-before-reveal + choices), spectrum (sweeper), provenance, ppm, flipper (serial
  transport), subfile (.sub parser), app (FastAPI console), __init__.
- Console: serves the touch shell end-to-end; commit-before-reveal enforced server-side;
  /api/spectrum honest 503 with no radio; canvas waterfall (one px per bin); JS tests via node.
- Curriculum shipped: M1 See the Invisible, M2 Turn It Up, M3 Know Your Instrument,
  M4 The Number on the Dial (Unit 1); M6 Resonance, M7 Build & Aim (Unit 3); M8 AM vs FM.
  All load + run through the console (smoke-verified logic-level).
- Shell preview published for taste review (claude.ai artifact).

## Decisions pending MC
- Shell taste: waterfall placeholder treatment, home nav depth, predict-instrument-free.
- Unblock tailnet SSH (acls tag:dev -> tag:fleet:22, or scoped OAuth token).

## Blockers
- HIL (real-radio) verification blocked: droplet -> spec5/pi5 SSH parked (D13/D14).

## Done / verified (logic-level on the droplet; NOT yet on real hardware)
- 252 tests green, ~93% cov, pristine; console runs all shipped lessons end-to-end;
  commit-before-reveal server-side; spectrum 503 honest; no module fabricates a signal/device.
- Known follow-ups: harden the loader's choices parser against YAML bool-coercion of keys
  (two choice bugs hit so far; both fixed, learnings logged); wire the panels; systemic HIL pass.
