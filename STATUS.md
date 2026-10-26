# SIGINT 101 — STATUS

## NEXT ACTION (resume here)
**Finish and land the Phosphor-Terminal console restyle.** The work is committed and pushed on
branch **`feat/phosphor-restyle`** (worktree: `/home/mc/workspaces/sigint-101-phosphor`), but the
latest commit (Russo One titles, ☭ labels, "comrade" voice, Back-button fix) is **not yet
render-verified**. Steps:
1. **Render-verify on nomad-b** at 800x480 (commands below). Confirm: Russo One loads on the big
   titles; the hammer-and-sickle **☭ (U+262D) renders as a glyph, not a tofu box** (if it tofus,
   swap the CSS `content:"\262D "` and the template `&#9773;` for an inline SVG hammer-sickle); the
   Back button shows "HOME" fully (no "HO..." clip); the predict screen still fits.
2. If good, **squash-merge into main and push**:
   `git -C /home/mc/workspaces/sigint-101 merge --squash feat/phosphor-restyle` → commit → push.
   Then remove the worktree (`cd /home/mc/workspaces` first) and delete the branch.
3. Then continue: the **"comrade" content pass** over `lesson_packs/*.yaml` (only chrome copy was
   done); the **units→lessons nav** (backend change, see below); remaining lessons + HIL.

## Render-verify recipe (no browser on the droplet — render on nomad-b's Chromium)
From the worktree `/home/mc/workspaces/sigint-101-phosphor`:
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
- The **predict** screen is JS-navigated (not a static shot). To see it, write a standalone
  `sigint101/static/_predicttest.html` that mirrors console.html's predict stage, render
  `/static/_predicttest.html`, then **delete it before committing**. (A prior one was used and removed.)
- Kill the server after: `pkill -f "[u]vicorn.sigint101"` (bracket avoids self-match).

## What the restyle contains (feat/phosphor-restyle)
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
  Predict chips are a horizontal auto-fit grid so the frame fits 800x480.
- **☭ (U+262D)** on every section-label eyebrow (CSS) and the predict question (template).
- **"comrade"** in the predict chrome copy (hint + committed note) — fuller pass pending.
- **Bug fixed** (found by the first real render): hidden stage-panels overlapped because
  `.stage-panel{display:flex}` beat the UA `[hidden]` rule — added `.stage-panel[hidden]{display:none}`.
- Captions raised to >=18px. **Verified so far:** `make selftest` green (290 tests, 94% cov, lint
  clean); home + lesson FRAME + predict rendered on nomad-b at 800x480 for the *base* commit
  (6a3c603). The *refinement* commit (bdf0507) is test-green but **not render-checked yet**.
- Commits: `6a3c603` (base) and `bdf0507` (refinements) on `feat/phosphor-restyle`; both on main's
  `dc94248`. Preview of all three directions (what MC chose from):
  https://claude.ai/artifact/1MnyjjtpDXizFRZVnDnzNh

## Both owner gates are CLEARED
- **Tailnet SSH open:** acls `tag:dev -> tag:fleet:22` added; droplet key authorized on all three
  fleet hosts. Droplet can SSH spec5, pi5, nomad-b.
- **Taste answers:** waterfall-with-no-radio = badged SAMPLE recording; home nav = units→lessons→
  lesson (not yet built — see pending); predict = frozen context frame (built); console look =
  Phosphor Terminal (being built).

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
- Repo: github.com/wittyreference/sigint-101 (public, MIT), main @ `dc94248`. Stack: FastAPI +
  Jinja2 + polling JS + offline CSS + SQLite + JSONL events, Pi 4 / Bookworm / aarch64, Chromium kiosk.
- 12 modules; console serves the shell end-to-end (commit-before-reveal server-side; /api/spectrum
  honest 503). 7 lesson packs (M1-M4, M6-M7, M8). Exam trainer backend on the verified NCVEC
  2026-2030 pool (409 + errata). `make selftest` = 290 tests, 94%, pristine; `make lint` clean.

## Pending work (after the restyle lands)
- "comrade" content pass over `lesson_packs/*.yaml` (the "you" references in lesson copy).
- **units→lessons nav** (home shows 6 unit tiles → a unit's lesson list → lesson). Backend change:
  app.py currently builds one tile per lesson with no unit grouping; needs a unit model + a
  `/unit/{id}` route (TDD).
- Wire gain/provenance/ppm panels + exam + legal UIs.
- Remaining lessons M5 (legal), M9-M18 + decode backends; counter-surveillance (Flock ESP32 on pi5
  + Rayhunter ingest); kiosk/provisioning/updater; Flipper FAP.
- Commit 3 NCVEC diagram JPGs for the exam UI; harden lessons.py choices parser vs YAML bool-coercion.

## Gotchas (learned this session)
- All `sigint101/` edits go through a worktree (worktree-gate). Docs (STATUS/CLAUDE.md) are outside
  the gate and land on main. Merge with **--squash**. Stage files by name. Never `--no-verify`.
  Git identity must be **Michael Carpenter <mc8805@outlook.com>** (repo-local, hook-enforced).
- Shell is **zsh**: unquoted `$var` does NOT word-split (`set -- $x` / `$NB cmd` break) — inline or use arrays.
- `pkill -f "pattern"` matches its own command line — use the bracket trick `"[p]attern"`.
- Render on nomad-b (no Chrome on the droplet). `rtl_power` "[R82XX] PLL not locked!" + "User
  cancel, exiting" are benign (appear on healthy nomad-b too).

## Done / verified
- Logic (droplet): 290 tests green, 94%, pristine.
- Real hardware (nomad-b): P0 acceptance passed; spectrum.py + ppm.py honesty verified on real SDR.
- Restyle base (6a3c603) render-verified on nomad-b at 800x480; refinements (bdf0507) test-green,
  render pending.
