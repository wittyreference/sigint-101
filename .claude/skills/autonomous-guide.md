---
name: autonomous-guide
description: Guide for unattended autonomous operation. Use when running headless, in CI/CD pipelines, or configuring Claude Code for non-interactive execution with quality gates.
---

# Autonomous Mode Guide

Both Claude Code and Feature Factory support autonomous mode for unattended operation. Autonomous mode pre-approves permission prompts while **keeping all quality gates enforced**.

## Quality Gates (Always Enforced)

Even in autonomous mode, these gates are never bypassed:

| Gate | Enforcement |
|------|-------------|
| TDD | Tests must fail first (Red), then pass (Green) |
| Linting | Must pass before commit |
| Coverage | 80% threshold |
| Credential safety | No hardcoded secrets (API keys, tokens, passwords) |
| Git safety | No `--no-verify`, no force push to main |

## Claude Code Path

Launch with pre-approved permissions:

```bash
./scripts/enable-autonomous.sh
```

Displays a warning, requires typing `I ACKNOWLEDGE THE RISKS` (read from stdin — pipe or type
it), then launches. It uses `claude --permission-mode acceptEdits` with a curated allow-list —
**never** `--dangerously-skip-permissions` — so the pre-approval never removes the quality/safety
gates above. Those are hooks (pre-commit identity/comment/review-evidence, pre-write
worktree/pipeline, TX safety, coverage) and fire regardless of permission mode. That is the whole
safety basis for pre-approving anything.

**Pre-approved (allow-list):**
- File operations (Read, Write, Edit, Glob, Grep)
- Test/lint: `make test`, `make elmer-selftest`, `.venv/bin/pytest`, `.venv/bin/ruff`
- Additive / read-only git: `git add`, `git commit`, `git status`, `git diff`, `git log`

**Explicitly denied (deny-list):**
- `rm`, `git push --force` / `-f`, `git reset --hard`, `sudo`

Deployment is **not** pre-approved (no deploy command is configured, and deploying unattended is
out of scope). Anything not on the allow-list still prompts. Session markers are saved to
`.claude/autonomous-sessions/`; the full transcript stays in Claude Code's own session store.

> **Deny-list verified 2026-09-07 on `claude` 2.1.261.** A sandboxed smoke test drove the real
> binary with these exact flags: an allowed edit auto-accepted, while edits to
> `.claude/settings*.json`, `githooks/`, `.claude/hooks/`, `.claude/rules/`, and `CLAUDE.md`
> (including a nested one) were refused — "denied by your permission settings". These flags are a
> **claude-version-coupled contract**: a future `claude` could rename a flag or change
> deny-precedence and the (stub-based) unit tests would not catch it. **Re-run
> `scripts/smoke-autonomous.sh` after any `claude` upgrade** to confirm the deny-list still enforces.

## Feature Factory Path

Run workflows without approval prompts:

```bash
# Interactive
npx feature-factory new-feature "task" --dangerously-autonomous

# CI/CD
FEATURE_FACTORY_AUTONOMOUS=true \
FEATURE_FACTORY_AUTONOMOUS_ACKNOWLEDGED=true \
npx feature-factory new-feature "task"
```

Removes budget/turn limits, auto-approves phase transitions. Session summary displayed at completion.

## Headless Path (CI/CD / Unattended)

> **Status (2026-09): NOT YET IMPLEMENTED.** `scripts/run-headless.sh` and its pre-defined tasks
> below are a design, not a shipped script — the interactive launcher above is built, the headless
> path is deferred. The `claude -p` patterns are documented here for when it is built.

Run Claude Code non-interactively with `claude -p`. No terminal UI, no human input -- true fire-and-forget. Acknowledgment via env var (CI/CD pattern).

```bash
# One-off task
CLAUDE_HEADLESS_ACKNOWLEDGED=true ./scripts/run-headless.sh "Run npm test and fix failures"

# Pre-defined task
CLAUDE_HEADLESS_ACKNOWLEDGED=true ./scripts/run-headless.sh --task test-fix

# Complex task from file
CLAUDE_HEADLESS_ACKNOWLEDGED=true ./scripts/run-headless.sh --prompt-file .meta/plans/validation-plan.md --max-turns 80

# CI/CD pipeline (low turn limit for quick checks)
CLAUDE_HEADLESS_ACKNOWLEDGED=true ./scripts/run-headless.sh --task validate --max-turns 20
```

**Pre-defined tasks:**

| Task | What it does |
|------|-------------|
| `validate` | Run /preflight, then configured test command with --bail |
| `test-fix` | Run tests, fix failures, commit fixes |
| `lint-fix` | Run linter, fix errors, commit fixes |
| `typecheck` | Run tsc --noEmit, fix type errors, commit fixes |

**Key differences from interactive autonomous mode:**
- Uses `claude -p` (non-interactive) -- no terminal UI, no countdown, no typing prompt
- Acknowledgment via `CLAUDE_HEADLESS_ACKNOWLEDGED=true` env var only
- Configurable `--max-turns` (default 30)
- Machine-readable output via `--output-format stream-json`
- Supports `--prompt-file` for complex multi-step plans
- Audit logs saved to `.claude/autonomous-sessions/headless-*.log`

For cross-cutting refactors using parallel sub-agents, see `scripts/headless-tasks/parallel-refactor.md` (requires companion spec at `.meta/refactor-spec.md`).

See `scripts/run-headless.sh --help` for full usage.

> **Note**: Agent teams should NOT be used in autonomous overnight runs. Teammates cannot resume sessions, so if a teammate's session is interrupted, its work is lost. Keep team tasks small enough to complete in one session.
