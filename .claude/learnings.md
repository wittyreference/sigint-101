# SIGINT 101 — Learnings

The running ledger of surprising discoveries. Capture inline the moment you learn something
non-obvious (doc-flywheel skill: Capture → Promote → Clear). Dated section headers, newest
first; bold the finding, then the context.

## 2026-10-26: Harness installed

- **Feature-factory harness seeded (TDD + self-learning + self-documenting, solo footprint).**
  Hooks, pipeline/self-learning commands, and six skills are in place; this ledger is the
  self-learning substrate — append findings here, promote stable ones to the relevant doc.

## 2026-10-26: parallel workers sharing a schema need identical field names
**Finding.** Two parallel workers built against a shared "choices" contract from separate
briefs: the console's `lessons.py` parser required `{key,label}` and fails closed (refuses an
entry missing `key`), while the M1 pack was authored with `{label,value}`. Each branch's suite
passed alone; once both merged, M1 refused to load and its tests went red — a latent contract
conflict a clean textual merge does not catch.
**How to apply.** When fanning out parallel workers that share a data contract (a field name,
an enum, a file format), pin the exact names in every brief, and run the FULL suite on the
integration branch before landing, never only per-branch.

## 2026-10-26: YAML coerces yes/no choice keys into booleans
**Finding.** Authoring M7's predict `choices` as `{key: yes, label: Yes}` / `{key: no, label: No}`
silently loaded as `key='True'`/`'False'` with labels `'True'`/`'False'`: PyYAML parses YAML 1.1,
where `yes|no|Yes|No|on|off|true|false` are booleans, and `lessons.py` then `str()`-coerces them.
The loader's key+label check still passed (both non-empty), so the test stayed green while the
learner would have seen "True / False" chips instead of "Yes / No".
**How to apply.** Quote any lesson-choice key or label that is a YAML 1.1 boolean word
(`yes/no/on/off/true/false`, any case) — e.g. `{key: "yes", label: "Yes"}`. A key/label
non-emptiness test will not catch the coercion; eyeball the parsed `choices` (or assert exact
values) whenever a chip reads True/False.

## 2026-10-26: ☭ (U+262D) renders via system fallback, not our bundled fonts
**Finding.** The eyebrow/predict hammer-and-sickle (`content:"\262D"` / `&#9773;`) renders fine on
nomad-b, but NOT because of our self-hosted fonts: the PT Mono @font-face `unicode-range` lists only
Latin/Cyrillic blocks and excludes U+262D, so the browser uses a system monospace fallback (DejaVu
Sans Mono, which has the glyph). The offline-font guarantee does not cover it.
**How to apply.** A glyph outside every bundled @font-face `unicode-range` depends on the renderer's
system fonts — it can tofu on a different target even when it renders on your dev box. Before kiosk
ship, confirm U+262D on the real Pi image's fontconfig, or bundle an inline SVG. Verify symbol
glyphs on the ACTUAL target, not just the render host.

## 2026-10-26: a nohup background process dies when the Bash tool call returns
**Finding.** `nohup .venv/bin/uvicorn ... &` from a Bash tool call looked launched but left no
process and no log — the backgrounded child is torn down when the tool's shell exits. Cost a couple
of confused retries.
**How to apply.** For a server/daemon that must outlive the call, use the harness background runner
(run_in_background), not `nohup ... &` inside a one-shot Bash call.

## 2026-10-26: predict is the tightest screen at 800x480 — keep prompts short
**Finding.** The predict prompt is rendered as the stage `<h1>` title; a 75-word prompt in the
Russo One display face pushed the tap chips and Commit button ~200px below the fold (Commit
unreachable on the fixed kiosk). Chips (72px touch) + Commit + eyebrow + frozen frame already fill
the ~360px stage area, leaving almost nothing for a multi-line title.
**How to apply.** Write predict prompts to ~35-40 words. The fix that landed: trim the prompt, hold
the title at the 18px floor (predict-scoped font-size), and reclaim chrome height (statusbar
`--bar-h` 72→66, rail `--rail-h` 48→40). Render-check every new lesson's predict screen at 800x480.
