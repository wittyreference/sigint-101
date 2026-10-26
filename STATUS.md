# SIGINT 101 — STATUS

## NEXT ACTION
Wire the lesson panels (gain / provenance / ppm) into the console routes + UI (pending MC's
taste call on the shell), then author Unit 2 (rules/legal) and Unit 3 (antennas). Hardware-in-the-
loop verification on spec5 is blocked until the tailnet SSH opens.

## Where things stand (2026-10-26)
- Repo live + green: github.com/wittyreference/sigint-101 (main). `make selftest` ~94% coverage,
  lint clean, pristine output.
- Backend complete: events (JSONL log), progress (SQLite), hardware (no-mock RTL-SDR probe),
  lessons (loader: intro/consolidation stages + commit-before-reveal + choices), spectrum
  (rtl_power sweeper), provenance (radio self-description), ppm (crystal error via 19 kHz pilot).
- Console: FastAPI app serves the touch shell end-to-end; commit-before-reveal enforced
  server-side; /api/spectrum returns an honest 503 with no radio; waterfall.js canvas renderer
  (one pixel per rtl_power bin); JS tests run from pytest via node.
- Curriculum: Unit 1 shipped (M1 See the Invisible, M2 Turn It Up, M3 Know Your Instrument,
  M4 The Number on the Dial). All four load and run through the console (smoke-verified).
- Touch-shell preview published for MC's taste review (claude.ai artifact).

## Decisions pending MC
- Three taste questions on the shell preview: waterfall placeholder treatment, home nav depth
  (units vs. lessons), and whether predict stays instrument-free.
- Unblock the tailnet SSH (acls rule tag:dev -> tag:fleet:22, or the scoped OAuth token) so
  HIL verification on spec5 can begin.

## Blockers
- HIL (real-radio) verification blocked: droplet -> spec5/pi5 SSH is parked (D13/D14).

## Done / verified (logic-level, on the droplet)
- Full suite green (~94% cov, pristine); console runs all 4 Unit 1 lessons end-to-end;
  commit-before-reveal enforced server-side; spectrum 503 honest; no module fabricates a
  signal or a device.
- NOT verified: anything on real hardware — needs SSH to spec5. The plan's lesson DoD is HIL.
