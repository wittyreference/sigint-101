# SIGINT 101 — STATUS

## NEXT ACTION
Two owner decisions gate the next work: (1) unblock the tailnet SSH so hardware-in-the-loop
verification on spec5 can begin (nothing is hardware-verified yet); (2) answer the three
shell-preview taste questions so the lesson panels (gain/provenance/ppm) and the exam-trainer
screen can be wired. Remaining build: M5 (legal card-sort), M9-M18 lessons + their decode
backends, counter-surveillance (Flock/ESP32 + Rayhunter ingest), kiosk/provisioning/updater,
the Flipper FAP, and the panel/exam/legal UIs.

## Where things stand (2026-10-26)
- Repo live + green: github.com/wittyreference/sigint-101 (main @ b90e589). `make selftest`
  290 tests, ~94% coverage, lint clean, pristine.
- Modules (12): events, progress, hardware (no-mock probe), lessons (intro/consolidation +
  commit-before-reveal + choices), spectrum (sweeper), provenance, ppm, flipper (serial
  transport), subfile (.sub parser), exam (Technician trainer engine), app (FastAPI console),
  __init__.
- Console: serves the touch shell end-to-end; commit-before-reveal enforced server-side;
  /api/spectrum honest 503 with no radio; canvas waterfall (one px per bin); JS tests via node.
- Curriculum (7 packs): M1-M4 (Unit 1), M6-M7 (Unit 3), M8 (AM vs FM). All run through the
  console (smoke-verified, logic-level).
- Exam trainer backend: the OFFICIAL NCVEC 2026-2030 Technician pool, 409/409 questions, the
  2026-02-19 errata verified by diffing the two releases (provenance + docx sha256 in the JSON);
  Leitner scheduler; 35-question practice exams, pass 26/35; fail-closed guard (ships disabled if
  the pool fails verification). UI deferred (taste-gated).
- Shell preview published for taste review (claude.ai/artifact/6sEVqu4YsWTfQEfP6H4qx2).

## Decisions pending MC
- Shell taste: waterfall placeholder treatment, home nav depth, predict-instrument-free.
- Unblock tailnet SSH (acls tag:dev -> tag:fleet:22, or scoped OAuth token).

## Blockers
- HIL (real-radio) verification blocked: droplet -> spec5/pi5 SSH parked (D13/D14).

## Follow-ups (logged, not yet done)
- Commit the 3 NCVEC diagram images (T1/T2/T3 JPGs) for the 12 figure questions, needed by the
  exam-trainer UI pass. Official URLs are in the exam worker's report.
- Harden lessons.py choices parser against YAML bool-coercion of keys (two choice bugs hit, both
  fixed; learnings logged) — coerce key/label to str in the parser.
- Wire the gain/provenance/ppm panels + exam + legal UIs (after the taste call).
- Systemic HIL pass on spec5 once SSH opens (the plan's lesson DoD).

## Done / verified (logic-level on the droplet; NOT yet on real hardware)
- 290 tests green, ~94% cov, pristine; console runs all shipped lessons end-to-end;
  commit-before-reveal server-side; spectrum 503 honest; exam pool verified; no module
  fabricates a signal, a device, or a question.
