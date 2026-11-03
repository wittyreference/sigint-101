# SIGINT-101 — resume, unfinished-work register, three-fold audit → hand-over (2026-11-02)

## Context
MC asked for more than a plain resume: find the work we left incomplete or abandoned without saying
so, then plan three audits — **technical** (code vs. intent, RF accuracy, performance), **UI/UX**,
and **curriculum** (does a learner come away able to run this hardware and work signals in the
field). **Hand-over is fixed at 2026-11-08, six days out.** Recon (3 read-only agents + my own checks)
already shows hand-over-level problems, so the audits feed a triage → fix sprint → hand-over, and
Flipper Phase 2 waits behind them (recommendation below).

## Live state (observed 2026-11-02 04:12 UTC, ssh spec5@100.108.20.20)
- **nomad-b healthy, no repeat crash:** up ~5.5 h, 32.6 °C, throttled=0x0, root ext4 clean, dmesg
  errors = one benign hid-multitouch line. The crash itself left **no log** (journal is volatile);
  the Oct 9 boot has no shutdown record → it died between Oct 31 and Nov 1 01:16.
- **Configs intact (STATUS NEXT 1):** meshtasticd active (2.7.26 beta); `config.yaml` mtime Jul 19
  (the gpsfix edit, not replaced; `.dpkg-dist` is the old Jun 29 one); `config.txt` has `spi0-0cs`,
  `enable_uart=1`, `gpio=4=op,dh`, `pps-gpio,gpiopin=17`, `miniuart-bt`. Oct 31 `apt upgrade` +
  `autoremove` removed `chromium-sandbox`; chromium 156 still installed.
- **Kiosk down** (no uvicorn/chromium); nomad-b checkout is on `ux-polish` @345fbfb.
- **Flipper not attached** (no /dev/ttyACM*) → fw 1.5.1 unconfirmed → app installs stay blocked.
- Main checkout is 1 commit behind origin/main (c22f7d9); this session's isolation guard refuses
  to touch it — pull it from a session started there.
- **Resume tree result:** Flipper not updated, ux-polish not panel-confirmed → the handover's
  default is Flipper Phase 2 (M36/M35). **I recommend parking it** until the S0 fixes land: on
  `main` a learner cannot complete a single lesson (the Complete button is only on unmerged
  `ux-polish`), and M30–M34 need your objects anyway.

## Already found (recon, spot-verified where marked ✓)
**Hand-over-level**
- ✓ Lesson completion impossible on main; progress bars stay 0 forever (ux-polish fixes it).
- ✓ Status bar invents values: battery always 100% (`base.html:37-40` default), PPM always "RAW",
  SNR always "—" — app never passes them. Breaks the no-mock rule.
- ✓ Tune +/- and tap-to-tune are display-only (`console.html:741`); ppm then measures the fake readout.
- ✓ Waterfall is browser-smoothed: `.waterfall` lacks `image-rendering: pixelated` (only
  `.gallery__img` has it, `app.css:973`) → interpolation, breaking the one-pixel-per-bin invariant.
- ✓ Hung decoder children never SIGKILLed: `_Pipeline.wait` swallows `TimeoutExpired` (a
  `SubprocessError`), so `_stop_locked`'s kill branch is dead code in ~7 decoders (`aprs.py:189`).
- Dead decoder reads as LIVE + empty (exit codes ignored outside Sweeper) → a dead radio shown as a
  quiet band; respawns every 1.5 s.
- M2 tells the learner to step gain — no gain control exists (`Sweeper.set_gain` unused).
- M6/M7/M8 copy talks 1090/915/airband but the instrument is stuck on FM; M8 needs <10 kHz detail
  on 125 kHz bins and promises audio (no audio path); M4 promises a one-tap correction that doesn't
  exist.
- ppm route bypasses `_claim_sdr` and passes the serial as `rtl_fm -d` index (`app.py:749-751`).
- Predictions never scored (no correct key), overwritable after reveal; several choice labels give
  the answer away ("No — silence proves nothing").
- No kiosk autostart, provisioning, personal-data scrub, quick-start, or updater (plan P5/P6).

**Unfinished / abandoned without a record** (plan deliverables never built, never mentioned again):
updater; provisioning + scrub + recipient quick-start; safe-shutdown + real battery; `safety.py`
antenna-before-TX guard + any TX path (CLAUDE.md calls it "the one retained guard" — it guards
nothing); reception badges / replay marks (CLAUDE.md invariant with no code); SigMF walkthroughs
R1–R6; retrieval cards, challenge tiers, capstone, "first 15 minutes", band map, M5 card sort +
"show me the law"; GPS + Wi-Fi/BLE survey lessons silently replaced; BLE Flock scan; DS3231/
nRF52840 kit; cold-boot panel + LoRa pin fixes (status unknown); your 11-01 written feedback list
(only 2 items made it into the record). Also: the 3 NCVEC figures were never extracted — they
live only in another session's /tmp (`…cfc7aa74…/scratchpad/ncvec_feb2026/pool.docx`); the render
harness lives only in /tmp; STATUS.md contradicts itself in ≥9 places; 8 dead branches (all landed).

## Course architecture: garden → rack → field (MC's question, 2026-11-02)
**Problem MC raised:** the course teaches inside its own UI, so once the learner leaves the kiosk
they land in a terminal with nothing connecting it to what they learned. Two learning-science
findings support MC's concern:
- **Transfer, "hugging and bridging"** (Perkins & Salomon): skills transfer when practice resembles
  the real setting (hugging) and when the mapping to that setting is made explicit (bridging). The
  console does neither; it hides the tools it drives.
- **Scaffolding has to fade** (cognitive apprenticeship, Collins/Brown/Newman; faded worked examples,
  Renkl). Guidance that helps a novice slows down someone who already knows the material (expertise
  reversal, Kalyuga). Every lesson today names the signal, pre-tunes the radio and picks the
  decoder, all the way through M28, so the learner is never asked to do these things alone.

**Where I disagree with "subtle":** this learner already lives in terminals. The shell is not the
hard part for them. The hard parts are RF judgment and knowing which of a dozen tools, with which
flags, does the job. So the course should show the real tools openly, from the first lesson.

**Recommendation: one console, three layers, guidance fading as the learner moves through them.**
1. **Garden: the lessons as they are.** Add one honest piece: a **command strip** on every
   instrument panel that shows the exact command the console is running right now. This is cheap
   because the decoders already build it (`Sweeper.command()` `spectrum.py:371`,
   `Decoder.command()` `decode.py:176`, `sstv.pipeline_commands`). For example, M10 displays
   `rtl_433 -f 433.92M -F json`. That command line is the bridge to the real tools.
2. **Rack: operator mode `/operate`.** It has the same look and the same panels, with the lesson
   script removed: free tuning (the real `/api/tune`), gain, a decoder picker, the command strip,
   and a reception log. This is the field instrument. In the field the touch panel is the only
   interface, so this mode has to exist. Most of it reuses work already on the fix list (real
   tuning and gain are S0 fidelity fixes) plus the existing panel routes. It must not become an
   SDR++ clone.
3. **Field: a closing "leave the garden" sequence, plus an on-screen field card.** It maps every
   panel to its upstream tool (rtl_433, dump1090-fa's web map, SDR++/GQRX, satdump, multimon-ng,
   qFlipper) and has the learner repeat a task they already did in the console using the real tool
   over SSH from their laptop.

**Fading inside the lessons:** from U5 on, lessons stop pre-tuning or stop naming the decoder, and
each unit ends with an **unnamed-signal solo** run in the rack. That solo is the plan's never-built
tier-C "solo hunt", and it restores the original plan's "independence" goal (M18 Signal-ID), which
Unit 6 theory displaced.

**Split for 11-08:**
- Before 11-08: the command strip and operator mode v1, built from the existing panels plus the
  fidelity fixes. Cost M.
- Via updates after hand-over: the field card, the "leave the garden" sequence and the per-unit
  solos.
- MC can move this cut line during triage.

**How this changes the curriculum audit (1c):** the audit scores each outcome on two levels,
"can do it in the garden" and "can do it in the rack or the real tool".

## Phase 0 — Secure + clean (no MC needed)
1. **Rescue /tmp-only assets** into a `chore-rescue` worktree: extract `image1-3.jpeg` from the
   NCVEC docx → the exam's figure path (wire T-1/T-2/T-3); move `render_scroll.py`, `render_fit.py`,
   `hil.sh` into `tools/render/` with ABOUTME headers + a `make render-check` target. Commit, push.
2. **Persistent journald on nomad-b** (`/var/log/journal`, `Storage=persistent`) so a repeat crash
   leaves evidence. Reversible one-line system change.
3. **Kiosk autostart on nomad-b** (plan P0 DoD "boots into kiosk"): commit `~/start-kiosk.sh` to the
   repo as `deploy/kiosk/` + a getty-autologin/labwc autostart unit; reboot-test it. Gets the panel up
   for your ux-polish look without a trip to the device tty.
4. **STATUS.md rewrite** (docs worktree → main): NEXT ACTION first, one true current-state section,
   the unfinished-work register as a table with a disposition column, stale sections deleted.

## Phase 1 — Three-fold audit (parallel subagents)
Three auditor subagents run concurrently, each seeded with the recon above (verify + deepen, don't
redo), each writing `docs/audit/2026-11-02-<name>.md`. Every finding carries: evidence (file:line,
measurement, or screenshot), learner impact, fix sketch, cost S/M/L, needs-MC/hardware flag, and a
severity: **S0** blocks hand-over / breaks a CLAUDE.md invariant / teaches something false ·
**S1** a lesson or panel misleads or asks the impossible · **S2** quality/perf/polish · **S3**
update-track. Then one adversarial verifier subagent re-checks every S0/S1 cold before triage.

**1a Technical.** Verify H/M findings as failing tests (they become the fixes' first tests).
Settle the rtl_power duplicate-bin question against `rtl_power.c` + a real capture. Measure on
nomad-b, not by estimate: uvicorn CPU/RSS during each instrument lesson's polling (60 s pidstat),
`/api/spectrum` payload + latency, ppm wall time, events.jsonl growth + SD write rate. Robustness
drills: SIGSTOP/SIGKILL a decoder child → what the UI shows; kill -9 uvicorn → orphaned rtl_* still
holding the dongle?; concurrent `_claim_sdr`. Suite health: `make selftest` + `make lint` on main,
ux-polish, flipper-module, and a trial integration of both (contract conflicts per
worktree-invariants). RF constants in code (SSTV 145.800, APT 137.1/NOAA-19, acarsdec gain units,
bias-tee claim, R828D). Duplication: the ~84%-identical decoder lifecycle — scope a shared base only
as an S3 recommendation (rewrite needs your OK).

**1b UI/UX.** Extend the render harness into a sweep: every route × every lesson stage × 3 themes
at a forced 800×480. Programmatic asserts: action button bottom ≤480; reading stages scroll,
instrument stages fit (Corrections rule); touch targets ≥72 px (Back 66 px is your accepted
exception); body text ≥18 px (58 of 80 font-size rules are <18); WCAG contrast per theme; red-night
leaks (rainbow waterfall `waterfall.js:32-39`, colour APT/SSTV images, dark-flash on load from
server-rendered `data-theme="dark"`). Flows: next-lesson discovery, completion → next, Back paths
(lesson Back skips the unit), no previous-stage, reload resume, raw-JSON 404s, hardware needs
invisible until mid-lesson, empty/no-radio states. Output a screenshot contact-sheet artifact.
Your 11-01 notes go in as input.

**1c Curriculum.** First write the target competency map (what "can operate this kit in the field"
means: sanity-check the instrument; find + identify an unknown signal; pick/aim an antenna; decode
the common services; judge legality; record + log with provenance; counter-surveillance
epistemics; mesh ops; Flipper capture; troubleshoot via internals; licence path), then trace each
outcome to lessons as taught / practised / assessed, and practised at which layer (garden /
rack / real tool) — the transfer gap MC raised is a first-class finding, not a footnote. Prerequisite check: concept first-use vs.
first-definition (dB used in M2 before defined; phase in M12, defined M22; front-end overload in M7,
explained M20; Nyquist after every decoder). Fidelity check: every instruction must be doable on the
console as built. Placement contradictions (M16/M27 both "close" U5; M23 "last lesson"; M20 "so
far…"; M25 "this unit"). Assessment validity (leaky choices, no scoring, observe captures nothing,
exam unlinked to lessons). Fact-check copy with sources. Load check (explains 130–294 words vs. the
fit budget). Output: competency matrix + gaps ranked by learner impact ÷ cost, split
before-11-08 / via-update.

## Phase 2 — Triage (you, one sitting)
I publish the merged ranked list as one artifact; you set the 11-08 cut line. Default I'll
recommend: every S0, plus S1s costed S/M. Unbuilt plan items each get a disposition: build now /
ship via update / dropped (your call, logged verbatim).

## Phase 3 — Fix sprint (subagent-heavy, worktree per cluster, TDD, render-verify, HIL)
Likely clusters, final set from triage:
1. **Land ux-polish** — rebase on main, PR, merge after your panel look; add "next lesson" on
   completion. Then rebase flipper-module (both touch `app.css`/`console.html`).
2. **Instrument honesty** — pixelated waterfall; decoder exit → honest "radio stopped"; kill
   escalation + process group / PDEATHSIG; status bar shows only real values (or hides them); ppm
   through `_claim_sdr` with the right device index.
3. **Lesson ↔ instrument fidelity** — real tuning (+/- → `/api/tune`), a gain control or M2 copy that
   matches, tune bands for M6–M8 or corrected copy, decoders honour the pack's `tune`.
4. **Copy + assessment** — placement contradictions, leaky predicts, fact fixes, explain lengths.
5. **Garden → rack** (see Course architecture): command strip on every instrument panel (argv
   from the running decoder, never a hardcoded string), then `/operate` v1 composing the existing
   panels with real tune/gain + a reception log. TDD the argv exposure; render-verify the rack.
6. **CLAUDE.md truthfulness** — invariants that name unbuilt features (badges, TX guard) either get
   built or reworded as "when built".
7. **Hand-over readiness — the recipient's own Recon ships (MC's call 2026-11-02); nomad-b stays
   our rig.** Start this cluster in parallel with Phase 1, because it's the long pole. Work:
   - An idempotent `deploy/provision.sh` covering the course, the venv and every system tool. Apt:
     rtl-433, multimon-ng, tcpdump. Built from source and pinned: dump1090-fa, acarsdec, redsea,
     rtl_ais.
   - tcpdump caps, wlan1 monitor mode, persistent journald and kiosk autostart.
   - Nothing of MC's on the box: no keys, tailnet, Wi-Fi credentials or git identity.
   - Prove it on a fresh SD card in nomad-b (keep nomad-b's current card untouched as the
     rollback), then cold-boot into the course.
   - Also: a recipient quick-start card, and a minimal git-based updater with rollback, tested on
     nomad-b.
   Arrival date assumed ~11/05 (original plan) until MC says otherwise.

## Phase 4 — Flipper (after Phase 3 S0s)
fw 1.5.1 + `ufbt launch` the 7 apps once you update and re-plug it; Phase 2 M36 BadUSB + M35 GPIO;
M30–M34 when you present real objects; merge PR #5 after ux-polish.

## What I need from you (one at a time, when reached)
1. ~~Which box ships~~ → the recipient's own Recon. Still need: its arrival date, and a spare
   SD card for nomad-b to prove the provision on.
2. Re-plug the Flipper on nomad-b + update to 1.5.1 in the app.
3. One look at the panel once autostart brings it up (ux-polish confirm).
4. Paste your 11-01 written feedback list.
5. The triage cut line.

## Verification
- Every fix: failing test first, `make selftest` (≥80% cov) + `make lint` pristine.
- UI: `make render-check` sweep green at 800×480 × 3 themes, screenshots in the contact sheet.
- HIL on nomad-b, captured back into the repo; kiosk survives a cold reboot into the course.
- Audit docs + STATUS committed and pushed; each phase ends with STATUS true and a resume prompt.
