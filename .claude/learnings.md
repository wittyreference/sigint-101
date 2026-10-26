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
