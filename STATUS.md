# SIGINT 101 — STATUS

## NEXT ACTION
Both owner gates are cleared — tailnet SSH is open (all three fleet hosts reachable) and the three
shell taste questions are answered. Immediate next: (1) **land the UI** — the 1980s-Soviet-terminal
restyle (three directions rendering now; MC picks, then port the winner into `app.css`'s three
themes) plus the taste calls: home navigates **units -> lessons -> lesson**, the offline waterfall
shows a **badged SAMPLE recording**, and Predict shows a dimmed, data-free **"frozen context"
instrument frame** (live data only after commit); then wire the gain/provenance/ppm panels + exam +
legal UIs. (2) **Continue per-module HIL on nomad-b**: ppm (M4) next, then the pi5 Flipper (Unit 3)
and nomad-b LoRa/GPS. Remaining build: M5 (legal card-sort), M9-M18 + decode backends,
counter-surveillance (Flock/ESP32 + Rayhunter), kiosk/provisioning/updater, the Flipper FAP.

## HIL targets (MC's call, 2026-10-26)
- **nomad-b = PRIMARY acceptance target** (hostname `spec5b`, 100.108.20.20). It is the stock
  Nomad 2 Recon twin — closest to the gift: built-in RTL2832U SDR + working antenna, MeshAdv HAT
  serial (`/dev/serial0`, `/dev/ttyAMA0`) for LoRa/GPS, working LoRa jack.
- **spec5 = degraded secondary** (100.90.68.20): busted RX antenna + dead LoRa jack. Still useful
  as a genuine "deaf receiver" fixture for the deaf-vs-empty honesty lesson (M3).
- **pi5 = Flipper target** (100.126.132.9), for Unit 3.
- Dev stays on the droplet (D14); these are acceptance targets reached over SSH.

## Where things stand (2026-10-26)
- Repo live + green: github.com/wittyreference/sigint-101 (main @ 4de85fc). `make selftest`
  ~290 tests, 94% coverage, lint clean, pristine (droplet).
- **HIL: P0 acceptance PASSED on nomad-b (real hardware):**
  - `make selftest` on real aarch64: Python suite green, 94.17% coverage. Only gap: the
    node-dependent JS meta-test (the panel JS runs in Chromium on-device, not node; it is verified
    on the droplet where node lives). node is a test tool, not a device runtime dependency.
  - Live app (`sigint101.app:app`): `/api/hardware` detects the real RTL2832U; `/api/spectrum`
    serves a live sweep (88-108 MHz, 257 bins, local FM station at 95.97 MHz, 29.6 dB SNR), with
    honest empty rows before the first pass; `/` and `/lesson/1` render 200; graceful stop releases
    the dongle (no leaked `rtl_power`).
  - `spectrum.py` parser (`parse_line` + `merge_chunks`) verified against genuine `rtl_power` CSV
    captured from both spec5 and nomad-b.
- Modules (12): events, progress, hardware (no-mock probe), lessons, spectrum (sweeper),
  provenance, ppm, flipper, subfile, exam, app, `__init__`.
- Console: serves the touch shell end-to-end; commit-before-reveal server-side; `/api/spectrum`
  honest 503 with no radio; canvas waterfall (one px per bin).
- Curriculum (7 packs): M1-M4 (Unit 1), M6-M7 (Unit 3), M8 (AM vs FM).
- Exam trainer backend: official NCVEC 2026-2030 Technician pool, 409/409 + 2026-02-19 errata
  verified; Leitner scheduler; 35-Q exams, pass 26/35; fail-closed. UI pending.

## Taste decisions (MC, 2026-10-26)
- Waterfall with no live radio: **badged SAMPLE recording** (real capture, marked SAMPLE, mints no
  reception badge).
- Home nav: **units -> lessons -> lesson** (6 unit tiles; drill into a unit's lessons).
- Predict: **frozen context frame** — dimmed, data-free instrument outline during predict; live
  data only after commit (MC's pick over the instrument-free default).
- Console look: **1980s Soviet terminal**, readability/functionality preserved (restyle in
  progress; three directions rendering for MC to pick).

## Access (un-parked 2026-10-26)
- Tailnet ACL: added `tag:dev -> tag:fleet:22` (claude-factory can SSH the fleet; narrows the
  MESH-15 "claude-factory destination-only" Q6a to the fleet only, plus the matching test edit).
- Droplet key authorized on spec5 (already present), and pushed onto pi5 and nomad-b via jump hosts
  (spec5 -> pi5; pi5 -> nomad-b).

## Follow-ups (logged, not yet done)
- Soviet-terminal console restyle: assemble the three rendered directions, MC picks, port into
  `app.css` three themes (+ the three taste calls above).
- spec5 busted antenna / dead LoRa jack: physical fix is MC's. Optionally repurpose spec5 as the
  M3 deaf-radio fixture.
- (optional) install node on nomad-b to make `make selftest` fully green on the acceptance box.
- Commit the 3 NCVEC diagram images (T1/T2/T3 JPGs) for the exam-trainer UI's 12 figure questions.
- Harden `lessons.py` choices parser against YAML bool-coercion of keys (coerce key/label to str).
- Wire the gain/provenance/ppm panels + exam + legal UIs (taste now decided).
- ppm (M4) HIL on nomad-b: measure the real FM-pilot ppm error on its built-in SDR.

## Done / verified
- Logic-level (droplet): ~290 tests green, 94% cov, pristine; console runs all shipped lessons
  end-to-end; commit-before-reveal server-side; spectrum 503 honest; exam pool verified; no module
  fabricates a signal, a device, or a question.
- Real hardware (nomad-b): P0 acceptance passed — see the HIL block above.
