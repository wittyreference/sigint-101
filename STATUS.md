# SIGINT 101 — STATUS

## NEXT ACTION (resume here)
**MC's call — M9 is fully done, including the live blink.** Open items:
- **M10 (FSK)** — a benign rtl_433 sensor. Needs `rtl_433` installed on nomad-b (not present;
  new system install → ask MC first). Uses the new tune band (ISM). *Recommended next.*
- **Small: commit the 3 NCVEC diagram JPGs (T-1/T-2/T-3)** so exam figure questions show their
  diagram instead of "not shown here yet". Pure asset add; no code.
Recommended: M10 (ask MC to approve the `rtl_433` install first). TDD; land `--squash`;
render-verify on nomad-b at 800x480.

**M9 live carrier-blink CAPTURED (observed, 2026-10-27).** The Flipper USB blocker was the
charge-only cable — MC swapped to a data cable and it enumerates (`0483:5740`, `/dev/ttyACM0`,
fw 1.4.3, region US). The droplet drove `subghz tx 0x00F7F7F7 433920000 350 60 0` on the Flipper
while nomad-b's SDR swept the 433.92 band: carrier reached **+22 dB above the noise floor** at the
433.92 bin (API), and the live `/lesson/9` observe waterfall rendered the carrier present ("LIVE·14")
and then, after keying stopped mid-capture, the quiet rows pushing in over it ("LIVE·24") — the
carrier that vanishes, on real hardware. Caveat: the near-field Flipper overloads the RTL front end,
so the lit rows smear broadband instead of a clean 433.92 column (honest overload + OOK splatter, not
invented signal); a crisp column needs the Flipper moved ~1 m off the whip (physical). Screenshots
delivered to MC. Full render recipe for a LIVE dynamic panel (stdlib-WebSocket DevTools capture with
a real-time wait — `--virtual-time-budget` snapshots an empty waterfall) in `.claude/learnings.md`.

**This session landed (11 features, all squash-merged; HIL where live):** Phosphor restyle + predict
fit (`a8bfef8`), units→lessons nav (`0e2fc54`), parser bool guard (`ca3ba48`), comrade voice
(`d2d4ae9`), M5 legal lesson (`bceb085`), exam study card (`aa2cc5a`), instrument-stage fit + gain/SNR
meter (`592bf84`), provenance panel (`53b0e79`), ppm panel (`cdbfb53`). All three instrument panels
(gain/provenance/ppm) built + HIL-verified on nomad-b (gain SNR 34 dB; provenance HEALTHY; ppm honest
no-pilot). main @ `fe16b0b`.

**Blocked on MC (physical, not code):** nomad-b recovers no FM stereo pilot — the strongest local
station (96.0 MHz, 17 dB) measured only a **1.5 dB pilot ratio** (<15 needed), so M4's real-ppm
measurement + the ppm-correction feature await the kit antenna / a known strong *stereo* station.

The units nav: home = 6 unit tiles (U1 Instrument .. U6 Under the Hood), `/unit/{id}` lists that
unit's loaded lessons, unbuilt units show an honest "no lessons here yet" (no lock, D16). Unit map
is `sigint101/units.py` (U1=M1-4, U2=M5, U3=M6-7, U4=M8-12, U5=M13-16, U6=M17-18). A new lesson pack
only shows up once its id is in that map — a contract test enforces every pack lands in a unit.
**Counter-surveillance (Rayhunter/Flock) is a separate track, NOT one of the 6 units** — surface it
as a 7th track tile only if MC asks.

## Render-verify recipe (no browser on the droplet — render on nomad-b's Chromium)
From the feature worktree (or main for a read-only render):
```
mkdir -p data
# Start the server via the HARNESS background runner (run_in_background), NOT `nohup ... &` — a
# nohup child dies when the Bash tool call returns (see learnings). Then on nomad-b (reaches the
# droplet over tailnet; user spec5 on host spec5b = nomad-b; 800x480 = kiosk size):
ssh spec5@100.108.20.20 '/usr/bin/chromium --headless=new --no-sandbox --disable-gpu \
  --hide-scrollbars --force-device-scale-factor=1 --window-size=800,480 --virtual-time-budget=6000 \
  --screenshot=/tmp/s.png http://100.92.112.54:8008/lesson/1'
scp spec5@100.108.20.20:/tmp/s.png <scratch>/   # then Read it
```
- `/` = home (tiles); `/lesson/1` = FRAME stage (eyebrow ☭ + Russo title + Back).
- **To render a hidden instrument stage (demo/try/observe)** — e.g. to check a panel or the copy fit —
  POST a prediction first so a sealed stage becomes the visible one, then screenshot `/lesson/N`
  directly (no iframe): `curl -X POST "http://127.0.0.1:8008/api/predict?lesson_id=N" -d
  "prediction=<any choice key>" -H "content-type: application/x-www-form-urlencoded"` → observe shows.
- **A LIVE panel (gain/provenance/ppm, the waterfall) needs real data → run the server ON nomad-b**,
  not the droplet (no SDR there). Full HIL recipe (checkout the branch at `/home/spec5/sigint-101`,
  hold the ssh open running uvicorn, warm the sweep, render localhost, restore) is in `.claude/learnings.md`.
  **For a LIVE DYNAMIC panel that must show accumulated real data (a lit waterfall), `--virtual-time-budget`
  does NOT work — it snapshots an empty "WAITING" panel because it races the real async sweeps. Use the
  stdlib-WebSocket `Page.captureScreenshot` + real-`sleep` recipe in `.claude/learnings.md` instead, and
  keep the signal live DURING the chromium session (the page re-tunes and clears the buffer on load).**
- The **predict** screen is JS-navigated (not a static shot). Best way to render it faithfully:
  write a throwaway `sigint101/static/_predictview.html` that loads `/lesson/1` in a same-origin
  iframe and flips the visible `.stage-panel` to the one with `data-stage="predict"` (the predict
  panel is server-rendered into the page, just `hidden`) — that screenshots the real production DOM.
  Add `getBoundingClientRect().bottom` of `.btn-commit` to confirm it is <=480. **Delete it before
  committing.** (Lesson YAML is cached at server start — restart uvicorn after editing a pack.)
- Kill the server after: `pkill -f "[u]vicorn.sigint101"` (bracket avoids self-match). Note: a
  `nohup ... &` from a Bash tool call dies when the call returns — run the server via the harness
  background runner (run_in_background) instead.

## What the restyle contains (landed on main @ a8bfef8)
- **`sigint101/static/app.css`** rethemed to the "Phosphor Terminal" (MC picked direction A of three):
  PT Mono everywhere (self-hosted OFL woff2 in `static/fonts/`, full Cyrillic, offline);
  double-line box-drawing borders; scanline+vignette+glow in the phosphor themes; blinking block
  cursor on the live control. Three themes keep their jobs: **dark** = green P1 CRT, **sunlight** =
  high-contrast dark-green-on-cream no-glow, **red-night** = red-on-black only (state by
  brightness+glyph+word). `data-theme` values are **dark / sunlight / red-night**.
- **Russo One** (OFL, self-hosted) on the large titles only (statusbar title, stage-panel title,
  home tile names) — legible Soviet-industrial display, not fake-Cyrillic.
- **Frozen-context predict frame** (MC's taste call): a dimmed, data-free instrument outline on the
  predict stage — orients to the band without revealing the answer; no canvas, nothing invented.
  Predict chips are a horizontal auto-fit grid.
- **☭ (U+262D)** on every section-label eyebrow (CSS `content`) and the predict question (template).
- **"comrade"** in the predict chrome copy (hint + committed note) — fuller lesson-copy pass pending.
- **Bug fixed** (found by the first real render): hidden stage-panels overlapped because
  `.stage-panel{display:flex}` beat the UA `[hidden]` rule — added `.stage-panel[hidden]{display:none}`.
- **Predict-fit fix** (this session): the predict prompt doubles as its `<h1>` stage title, so at
  800x480 the long question pushed the chips + Commit button ~200px off-screen (Commit unreachable).
  Fixed per MC's calls — (a) trimmed lesson 1's predict prompt to its setup + question, (b) held the
  predict title at the 18px floor (predict-scoped `font-size`), (c) reclaimed chrome height from the
  statusbar (`--bar-h` 72→66) and stage rail (`--rail-h` 48→40). Commit button now lands at y=478.
  Tradeoff MC accepted: the Back button is now 66px tall (just under the 72px touch floor).
- Captions raised to >=18px. **Verified:** `make selftest` green (290 tests, 94% cov, pristine);
  `make lint` clean; home + FRAME + PREDICT render clean on nomad-b at 800x480; Russo One (Latin +
  Cyrillic) loads; ☭ renders as a glyph. Preview of all three directions (what MC chose from):
  https://claude.ai/artifact/1MnyjjtpDXizFRZVnDnzNh

## Both owner gates are CLEARED
- **Tailnet SSH open:** acls `tag:dev -> tag:fleet:22` added; droplet key authorized on all three
  fleet hosts. Droplet can SSH spec5, pi5, nomad-b.
- **Taste answers:** waterfall-with-no-radio = badged SAMPLE recording; home nav = units→lessons→
  lesson (not yet built — see pending); predict = frozen context frame (built); console look =
  Phosphor Terminal (LANDED @ a8bfef8).

## HIL targets (MC's call) — nomad-b is PRIMARY
- **nomad-b** (hostname `spec5b`, `100.108.20.20`, user `spec5`) = PRIMARY acceptance target: stock
  Nomad 2 Recon twin, built-in RTL2832U + working antenna, MeshAdv HAT serial (`/dev/serial0`,
  `/dev/ttyAMA0`), working LoRa jack. **P0 acceptance PASSED** here: live `/api/hardware` +
  `/api/spectrum` (real FM sweep, ~96 MHz, ~29 dB SNR), pages 200, clean dongle release.
- **spec5** (`100.90.68.20`) = degraded secondary: **busted RX antenna + dead LoRa jack**. Useful
  as a "deaf receiver" fixture for the M3 deaf-vs-empty lesson.
- **pi5** (`100.126.132.9`, user `mc`) = Flipper target, but **no Flipper attached** (it has an
  ESP32 / Espressif USB-JTAG on `/dev/ttyACM1` — good for the later counter-surveillance track).
- Dev on the droplet (D14); these are acceptance targets over SSH. **No browser on the droplet** —
  render on nomad-b's Chromium.
- **ppm (M4):** panel LANDED (`cdbfb53`); `ppm.py` + the panel honesty verified on hardware
  (refuses a ppm with no clear pilot). Happy path still blocked — **nomad-b recovers no FM stereo
  pilot**: a fresh HIL measured even the strongest local station (96.0 MHz, 17 dB) at only a
  **1.5 dB pilot ratio**, far below the 15 dB needed. This is reception, not code — MC's physical
  call (kit antenna / a known strong *stereo* station). The real-ppm measurement awaits a pilot.

## Where the build stands
- Repo: github.com/wittyreference/sigint-101 (public, MIT), main @ `8ed4c3e`. Stack: FastAPI +
  Jinja2 + polling JS + offline CSS + SQLite + JSONL events, Pi 4 / Bookworm / aarch64, Chromium kiosk.
- 13 modules (added `units.py`); console serves the shell end-to-end (commit-before-reveal server-side; /api/spectrum
  honest 503). 9 lesson packs (M1-M9); a pack can declare its own tune band (`tune:`) so the
  instrument starts off the FM default. Exam trainer backend on the verified NCVEC
  2026-2030 pool (409 + errata), reachable from home. `make selftest` = 361 tests + 17 node (JS) tests, 94%, pristine; `make lint` clean.

## Pending work
- Commit the 3 NCVEC diagram JPGs (T-1/T-2/T-3) so figure questions in the exam show their diagram
  (today they read "not shown here yet").
- Capture M9's live carrier-blink (needs a 433.92 MHz OOK source transmitting). Flipper not
  enumerating over USB on nomad-b (no /dev/ttyACM*, lsusb lacks 0483:5740 — likely a charge-only
  cable); fix it to drive its TX over the CLI for an automated blink capture.
- Remaining lessons M10-M18 + decode backends (M10 FSK needs `rtl_433` on nomad-b — not installed,
  a new system install); counter-surveillance (Flock ESP32 on pi5 + Rayhunter ingest);
  kiosk/provisioning/updater; Flipper Lesson Companion FAP (Pulse Scope, own worker track).

## Gotchas (learned this session)
- All `sigint101/` edits go through a worktree (worktree-gate). Docs (STATUS/CLAUDE.md) are outside
  the gate and land on main. Merge with **--squash**. Stage files by name. Never `--no-verify`.
  Git identity must be **Michael Carpenter <mc8805@outlook.com>** (repo-local, hook-enforced).
- **Worktree setup:** `git worktree add ../sigint-101-<name> -b feat/<name>`, then
  `ln -s /home/mc/workspaces/sigint-101/.venv <worktree>/.venv` and `mkdir <worktree>/data`.
  No editable install — `pytest.ini` has `pythonpath = .`, so running `.venv/bin/pytest` FROM the
  worktree imports the worktree's `sigint101/` (not main's). `.venv`/`data/` are gitignored; never
  `git add` the symlink. Lesson YAML is cached at server start — restart uvicorn after editing a pack.
- Shell is **zsh**: unquoted `$var` does NOT word-split (`set -- $x` / `$NB cmd` break) — inline or use arrays.
- `pkill -f "pattern"` matches its own command line — use the bracket trick `"[p]attern"`.
- Render on nomad-b (no Chrome on the droplet). `rtl_power` "[R82XX] PLL not locked!" + "User
  cancel, exiting" are benign (appear on healthy nomad-b too).
- **☭ (U+262D) renders via a SYSTEM-FONT FALLBACK, not our bundled fonts.** The PT Mono @font-face
  `unicode-range` excludes U+262D, so the glyph comes from whatever monospace the renderer falls
  back to (DejaVu Sans Mono on nomad-b, which has it). This works on nomad-b but is NOT guaranteed
  on the actual kiosk Pi if its fontconfig lacks a glyph for U+262D — it would tofu. **TODO before
  kiosk ship:** confirm U+262D on the real kiosk image, or bundle an inline SVG hammer-sickle.
- **Predict is the tightest screen at 800x480.** Keep predict prompts SHORT (~35-40 words): the
  prompt is the stage `<h1>`, and chips (72px touch) + Commit + frozen frame leave little room. New
  lessons' predict copy should be written to fit; the content pass should check each one renders.

## Done / verified
- Logic (droplet): 361 tests green (+ 17 node JS tests), 94%, pristine.
- M9 "The Carrier That Vanishes" (OOK) + per-lesson tune band LANDED (`8ed4c3e`): a pack can
  declare `tune: {center_hz, span_hz, step_hz?}`; lessons.py validates it against the tuner reach +
  sweep limits at load, the lesson route points the dial/step at it, and waterfall.js POSTs the band
  to /api/tune once on load (packs with no band keep the FM default — the 8 existing lessons are
  untouched). M9 is a six-stage intro tuned to 433.92: a keyed OOK source blinks the carrier fully
  to the noise floor; message = pattern of bursts/gaps; two-view idea (waterfall=frequency, Flipper
  Read RAW=time). TDD throughout (band 6, route 2, render 2, parseTuneBand 2 node, M9 content 5).
  **HIL-verified on nomad-b at 800x480**: the live sweep retunes to 432.92-434.92 MHz on the real
  SDR (dial 433.920, 20 kHz step); frame, predict (41-word prompt fits, 3 chips + Commit on screen),
  observe all render clean; the band shows honest-empty with nothing keying. **Not yet captured:**
  the live carrier-blink (needs a 433 OOK source transmitting — see NEXT ACTION), like M4's deferred
  happy-path. Also unblocks later non-FM lessons (ADS-B 1090, GPS).
- Exam-nav LANDED (`84bd0b1`, MC-reviewed): the licence exam trainer now has a home affordance —
  a full-width CTA under the unit grid ("Licence exam / Practice for your Technician ticket →")
  that lands on `/exam` (which links on to the 35-Q practice exam). MC's call: a full-width bar
  below the grid, not a 7th tile — the trainer is a cross-cutting licence track, not a curriculum
  unit, so it reads apart (accent double border, inline-SVG diamond glyph, no U-code/progress bar).
  TDD (1 route test + 1 template test). **HIL-verified on nomad-b at 800x480** (dark theme): grid +
  CTA fit with no overflow, both tile rows intact, glyph + arrow render. red-night/sunlight inferred
  (shared theme tokens, no state-by-colour). `/exam` returns 200.
- Exam-sim (scored 35-question practice exam) LANDED (`32ec4a0`, MC-reviewed): `/exam/sim` draws a
  35-Q sheet (one per NCVEC group) and serves the current question with a running score; answering
  reveals the right option + the learner's miss (commit-before-reveal) and advances; `/exam/sim/
  results` scores it — PASS/NOT YET at 26/35 (74%), the percentage, and a per-subelement breakdown
  that flags the areas not aced; Retake starts fresh. The in-progress sim persists in a one-row
  `sim` table in exam.db (survives power loss); each answer also feeds the Leitner store, so a sim
  sharpens the study card. MC's call: answer-then-advance, not all-35-then-score. TDD (sim store +
  routes + templates). **HIL-verified on nomad-b at 800x480**: the question card, the review (correct
  marked, miss flagged), and the results screen (13/35 NOT YET, pass line, 10-cell subelement grid)
  all render clean, Back always visible. Study card links to the sim; neither is on home yet (pending).
- Instrument-lesson copy trim + eyebrow fit LANDED (`85d126d`, MC-reviewed): trimmed the demo/try/
  observe narration (and over-long predict prompts) on M1/M3/M4/M6/M7/M8 the way M2 was, 20-43% off
  each demo, every teaching beat kept. Render-verify on nomad-b exposed the real constraint — the
  instrument narration is a thin scroll band (waterfall 120px + tune + title eat ~480px), and the
  eyebrow reprinted the full lesson `concept` on EVERY stage, leaving only ~27px on a radio lesson.
  MC's call: drop the concept from the eyebrow on the hardware instrument stages (demo/try/operate/
  observe) — it already rode the frame; text/no-hardware stages keep it. HIL-verified: the M7
  narration band grew 27px -> 65px (one line -> ~two) across every instrument stage. MC chose NOT to
  gut the dense demos, so M6/M7/M8 still scroll by design (content-faithful, like the shipped M2).
  TDD for the template change (2 eyebrow tests). 327 tests green.
- Units→lessons nav LANDED on main (`0e2fc54`): home shows 6 unit tiles, `/unit/{id}` lists a unit's
  lessons, unbuilt units render an honest empty page (no lock). Render-verified on nomad-b at 800x480
  (home, /unit/1 Instrument, /unit/5 Decode empty); 304 tests green, lint clean. TDD throughout.
- Lesson-pack loader hardened (`ca3ba48`): `parse_lesson` now refuses a predict choice key/label
  that YAML coerced to a bool (unquoted yes/no/true/false), so an authoring slip fails loud at load
  instead of becoming a silent "True"/"False" chip. TDD; 304 tests green.
- Light-touch "comrade" voice pass LANDED (`d2d4ae9`): one address at the frame opening + the
  "your turn" beat of all 7 packs; teaching copy + predict prompts untouched. MC's call: light, not
  full persona. Render-verified lesson 2 FRAME on nomad-b. (Fuller lesson copy comes with new lessons.)
- M5 "The One Rule" legal lesson LANDED (`bceb085`, MC-reviewed): U2's lesson, no-RF; listening is
  broad/lawful, transmitting is licensed (D16 framing, jurisdiction caveat). Shipped with a general
  console fix — a no-hardware lesson renders as text with a chips-only predict (gated on
  `lesson.needs_hardware`). TDD; render-verified M5 frame + predict on nomad-b. Unit 2 is no longer
  empty in the nav.
- Exam trainer study card LANDED (`aa2cc5a`, MC-reviewed): `/exam` serves the weakest spaced-
  retrieval card; tap + commit → `/exam/review` marks the correct option (bright + check) and a
  wrong pick (dashed + cross), with the FCC ref. Reuses the predict-chip pattern + the Leitner
  store. TDD; render-verified on nomad-b. Exam-sim mode + results screen + 3 diagram JPGs deferred.
- Gain/SNR meter (M2) + instrument-stage fit LANDED (`592bf84`, MC-reviewed): a vertical level meter
  (floor/peak lines, shaded SNR gap) beside the waterfall, live off the same sweep. The demo/try/
  observe stages now fit 800x480 — Next-step on the tune strip, scrolling narration (they were
  over-subscribed and never render-verified). **HIL-verified on nomad-b**: the live meter read
  PEAK -14.7 / FLOOR ~-49 / SNR 34.0 dB off a real FM sweep.
- ppm panel (M4) LANDED (`cdbfb53`, MC-reviewed): measure-on-demand — the Measure button captures
  the tuned station's 19 kHz pilot via `/api/ppm/measure` and shows the crystal error, or an honest
  "no clear pilot". HIL-verified on nomad-b: the measurement runs and honestly refuses (no
  recoverable pilot there yet — see HIL targets). All three instrument panels (gain/provenance/ppm)
  are now built.
- Provenance panel (M3) LANDED (`53b0e79`, MC-reviewed): a verdict-first panel (HEALTHY/DEGRADED/
  NO BASELINE/NO RADIO) off the live reference vs. a captured baseline, with a Capture button; new
  `/api/provenance` + `/api/provenance/baseline` routes. **HIL-verified on nomad-b**: HEALTHY off a
  real sweep (REF -13.2 vs base -12.5, temp 37C, power OK).
- Real hardware (nomad-b): P0 acceptance passed; spectrum.py + ppm.py honesty verified on real SDR.
- Phosphor-Terminal restyle LANDED on main (`a8bfef8`): home + FRAME + PREDICT render-verified on
  nomad-b at 800x480; Russo One (Latin + Cyrillic) loads; ☭ renders as a glyph (via system
  fallback — see Gotchas); predict fits with the title at 18px; tests 290 green/94%, lint clean.
