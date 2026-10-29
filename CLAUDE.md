# CLAUDE.md — SIGINT 101 working agreement

A self-contained, touch-screen, learn-by-doing SIGINT course that takes a beginner to competent
signals-intelligence fundamentals by driving the real radios of a SpecFive Nomad 2 Recon paired
with a Flipper Zero.

Authoritative detail lives in the plan (`/home/mc/.claude/plans/eager-spinning-frost.md`) and
`STATUS.md` (living state). This file is the lean agreement; when they disagree, the plan wins.

## Dev pipeline

Every feature moves through: **architect -> spec -> test-gen -> dev -> review -> test -> docs**
(slash commands of the same name). Work one feature/module per worktree.

**TDD is non-negotiable** for any code with logic:
1. Write the failing test first (`tests/test_<module>.py`).
2. Run it; watch it fail for the *right* reason.
3. Write the minimal code to make it pass.
4. Refactor green. Never edit a test to fit a bug.

- **Acceptance gate:** `make selftest` (`pytest --cov=sigint101 --cov-fail-under=80`) green with
  **pristine output** — no warnings, no stray prints. Lint clean: `make lint` (`ruff check .`).
- HIL (hardware-in-the-loop) steps are "done" only when the real run is observed on the real
  target and its output/artifact is captured back into the repo.

## Code conventions

- **ABOUTME:** every `.py` starts with exactly two comment lines:
  ```python
  # ABOUTME: <what this file does>
  # ABOUTME: <key behavior or context>
  ```
  (Enforced by the pre-write hook; `.py` only, source files.)
- **Test naming:** `tests/test_<module>.py`.
- Smallest change that works. Match surrounding style. Keep comments; use evergreen names (never
  `new`/`improved`/`enhanced`). No fake/mock mode in the product — real code only; test doubles
  live at hardware/app boundaries, never as proof.
- Layout: `sigint101/` (FastAPI console, lesson engine, hardware probe), `lesson_packs/` (one file
  per lesson), `tests/` (unit, integration, e2e).

## Capability posture (D16 — no artificial gates)

The recipient is an experienced ethical hacker, new to radio but not to ethics. The device is a
capable instrument, not a locked toy.

- **Enable the full hardware:** transmit, LoRa/Meshtastic, active scanning, every band the radios
  reach. No call-sign gate, no fail-closed questionnaire, no "you can't use LoRa" wall.
- **Teach law and ethics as knowledge, never as a functional lockout.** Surface legal facts where
  relevant; the exam trainer is an on-ramp to licensed operation, not a lock.
- **The one retained guard** is a **non-blocking antenna-before-TX hardware-protection warning**
  (so the LoRa PA is never keyed into an open jack). It protects the gift, not an ethics gate.

## RF & teaching invariants (never violate)

- **Never render invented signal.** The canvas waterfall draws **one pixel per `rtl_power` bin** —
  no interpolation, no synthetic fill. If there is no signal, show the empty band honestly.
- **The hardware probe is no-mock.** It detects a real RTL-SDR or admits it can't find one. It
  never fakes a device.
- **Recorded walkthroughs are labelled "replay"** and mint **no reception badge**.
- **Badges only for real receptions with provenance** (an `events`-logged reception). Offline/
  recorded earn a visibly different "replay/seen" mark — that distinction is itself the lesson.
- **Counter-surveillance honesty:** "no alert never means no camera." A heuristic can't prove
  catcher vs. a new legit tower. Teach the epistemics; never imply false certainty.

## Touch UI constraints

- Fixed **800x480**; one task per screen; **Back always visible**.
- Touch targets **72-80 px**; body text **>=18-20 px**.
- Three themes: **dark / sunlight / red-night**.
- **Keyboard-free prediction input** (tap + long-press; no on-screen keyboard for the predict gate).

## Infra & workflow

- **Dev runs on the droplet** (D14). spec5 (SDR) and pi5 (Flipper) are **acceptance-test targets
  only**, reached over SSH — never dev hosts. No Claude session runs on the Pis; the droplet runs
  the hardware command remotely and captures the output back.
- **All repo edits go through linked worktrees.** The global pre-write hook blocks writes to the
  main checkout. Create a worktree, edit there, commit there, then remove it from main.
- **Stage files by name** — never `git add -A`.
- **Never `--no-verify`.** Fix what a commit hook flags; never bypass by rewriting history.
- **Merge with `--squash`, not `--merge`** — a GitHub-side merge commit stamps the account's
  primary email and bypasses the local identity hook; squash keeps the committed author.
- Self-learning ledger: append non-obvious findings to `.claude/learnings.md` as you hit them;
  grep it during `architect`/`spec` so the fleet doesn't repeat mistakes.

## Project facts

- **Repo:** github.com/wittyreference/sigint-101 (public, MIT). Nothing personal to MC committed.
- **Git identity:** Michael Carpenter <mc8805@outlook.com>. Check `git config user.email` before
  your first commit each session; if it differs, stop and say so.
- **Plan:** `/home/mc/.claude/plans/eager-spinning-frost.md` (authoritative detail).
- **Living state:** `STATUS.md` (NEXT ACTION first, then done, blockers). Keep it true after every
  merge or push.
- **Stack:** FastAPI + Jinja2 + polling JS + offline CSS + SQLite progress + append-only JSONL
  events, on a Pi 4 / Bookworm / aarch64 board (Chromium `--kiosk`).
- **Hand-over:** 2026-11-08.

## Corrections

- When a decision needs MC, **ask ONE focused question at a time** and act on the answer — not a
  multi-option menu dump. (Session 2026-10-26.)
- **Be autonomous: don't hand back checks or wait.** Finish the phase, move to the next thing, and
  only pause for what genuinely NEEDS MC's input (taste, a physical/hardware step, an irreversible
  action). This reinforces the global "do it without asking" rule. (Session 2026-10-26.)
  - **Sharpened 2026-10-29:** never gate a reversible build step by asking "should I start / build /
    go?" — just do it and report. Default to **subagent-heavy** execution: fan research and
    independent chunks out to subagents. Ending a turn with "Want me to go?" is the exact ping-pong
    MC called out. One unavoidable tap remains (EnterWorktree); flag it, then stay heads-down.
- **nomad-b (user `spec5` @ 100.108.20.20, host `spec5b`) is the WORKING HIL target**; spec5
  (100.90.68.20) is the deaf one (busted RX antenna). Don't conflate them. (Session 2026-10-26.)
