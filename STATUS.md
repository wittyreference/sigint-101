# SIGINT 101 — STATUS

## NEXT ACTION (resume here)
**Two features LANDED on main this session:** Phosphor-Terminal restyle (`a8bfef8`) and the
**units→lessons nav** (`0e2fc54`) — both squash-merged, branches + worktrees removed, render-verified
on nomad-b at 800x480. Pick up the pending content/lessons work — recommended next:
1. **Remaining lessons + HIL** (see Pending): M5 legal, M9-M12, U5/U6, decode backends,
   counter-surveillance (Rayhunter panel + Flock) — none built yet. NOTE: predict prompts must stay
   short (~35-40 words) or they overflow predict's 800x480 — render-check each one. Choice keys/labels
   that are YAML booleans (yes/no/true/false) now fail loud at load — quote them.
2. **UX specs needed from MC** before building: gain/provenance/ppm panels, exam UI, legal UI
   (backends exist; exam pool is 100% done). These are touch-UI layout/taste calls.

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
nohup .venv/bin/uvicorn sigint101.app:app --host 0.0.0.0 --port 8008 >/tmp/uv.log 2>&1 &
# then on nomad-b (reaches the droplet over tailnet; 800x480 = kiosk size):
ssh spec5@100.108.20.20 '/usr/bin/chromium --headless=new --no-sandbox --disable-gpu \
  --hide-scrollbars --force-device-scale-factor=1 --window-size=800,480 --virtual-time-budget=6000 \
  --screenshot=/tmp/s.png http://100.92.112.54:8008/lesson/1'
scp spec5@100.108.20.20:/tmp/s.png <scratch>/   # then Read it
```
- `/` = home (tiles); `/lesson/1` = FRAME stage (eyebrow ☭ + Russo title + Back).
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
- **ppm (M4):** `ppm.py` honesty verified on hardware (refuses a ppm with no clear pilot); happy
  path blocked — nomad-b currently recovers **no FM stereo pilot** (antenna; MC's physical call to
  fix — try the kit antenna or a known strong local stereo station).

## Where the build stands
- Repo: github.com/wittyreference/sigint-101 (public, MIT), main @ `0e2fc54`. Stack: FastAPI +
  Jinja2 + polling JS + offline CSS + SQLite + JSONL events, Pi 4 / Bookworm / aarch64, Chromium kiosk.
- 13 modules (added `units.py`); console serves the shell end-to-end (commit-before-reveal server-side; /api/spectrum
  honest 503). 7 lesson packs (M1-M4, M6-M7, M8). Exam trainer backend on the verified NCVEC
  2026-2030 pool (409 + errata). `make selftest` = 304 tests, 94%, pristine; `make lint` clean.

## Pending work
- Wire gain/provenance/ppm panels + exam + legal UIs.
- Remaining lessons M5 (legal), M9-M18 + decode backends; counter-surveillance (Flock ESP32 on pi5
  + Rayhunter ingest); kiosk/provisioning/updater; Flipper FAP.
- Commit 3 NCVEC diagram JPGs for the exam UI.

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
- Logic (droplet): 304 tests green, 94%, pristine.
- Units→lessons nav LANDED on main (`0e2fc54`): home shows 6 unit tiles, `/unit/{id}` lists a unit's
  lessons, unbuilt units render an honest empty page (no lock). Render-verified on nomad-b at 800x480
  (home, /unit/1 Instrument, /unit/5 Decode empty); 304 tests green, lint clean. TDD throughout.
- Lesson-pack loader hardened (`ca3ba48`): `parse_lesson` now refuses a predict choice key/label
  that YAML coerced to a bool (unquoted yes/no/true/false), so an authoring slip fails loud at load
  instead of becoming a silent "True"/"False" chip. TDD; 304 tests green.
- Light-touch "comrade" voice pass LANDED (`d2d4ae9`): one address at the frame opening + the
  "your turn" beat of all 7 packs; teaching copy + predict prompts untouched. MC's call: light, not
  full persona. Render-verified lesson 2 FRAME on nomad-b. (Fuller lesson copy comes with new lessons.)
- Real hardware (nomad-b): P0 acceptance passed; spectrum.py + ppm.py honesty verified on real SDR.
- Phosphor-Terminal restyle LANDED on main (`a8bfef8`): home + FRAME + PREDICT render-verified on
  nomad-b at 800x480; Russo One (Latin + Cyrillic) loads; ☭ renders as a glyph (via system
  fallback — see Gotchas); predict fits with the title at 18px; tests 290 green/94%, lint clean.
