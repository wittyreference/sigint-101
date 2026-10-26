---
name: factory-triage
description: Diagnose factory failures — stalled agents, headless crashes, eval metric degradation, and hook enforcement issues. Use when a workflow, headless session, or validation run fails unexpectedly.
---

# Factory Triage

Systematic diagnosis of Feature Factory failures. Work through these decision trees before diving into code.

## Quick Triage Decision Tree

| Symptom | First Check | Tool |
|---------|------------|------|
| Workflow phase failed | Phase result in session state | `cat .feature-factory/sessions/<id>.json` |
| Headless session exited early | Audit log for last tool call | `tail -50 .claude/autonomous-sessions/headless-*.log` |
| Agent stalled (no progress) | Stall detection events | Check event logs for stall patterns |
| Hook blocked a write/commit | Hook stderr output | Check terminal output for `[hook]` prefix |
| Eval metrics degraded | Regression comparison | Compare current metrics against baseline |
| Validation score dropped | Score trend | Review validation output history |

## Diagnosing Workflow Failures

### 1. Check the session state file

```bash
# Find the latest session
ls -t .feature-factory/sessions/*.json | head -1 | xargs cat | jq '.status, .error, .currentPhaseIndex'
```

### 2. Check phase results

```bash
# See which phases completed and which failed
cat .feature-factory/sessions/<id>.json | jq '.phaseResults | to_entries[] | {agent: .key, success: .value.success, error: .value.error}'
```

### 3. Common failure patterns

| Phase | Failure | Root Cause | Fix |
|-------|---------|------------|-----|
| architect | `approved: false` | Design rejected | Review architect output, re-run with clearer requirements |
| test-gen | `allTestsFailing: false` | Tests pass before implementation | Tests may reference existing code — check for name collisions |
| dev | TDD VIOLATION | tdd-enforcement hook blocked | test-gen phase didn't create failing tests — re-run test-gen |
| dev | `allTestsPassing: false` after retries | Implementation couldn't pass tests | Check if tests are correct; tests may need spec adjustment |
| qa | `verdict: FAILED` | Coverage below 80% or security issues | Check coverage gaps and security scan output |
| review | `verdict: REJECTED` | Code quality issues | Read review issues, re-run dev with feedback |

## Diagnosing Headless Failures

### Common headless-specific issues

| Issue | Symptom | Diagnosis |
|-------|---------|-----------|
| Session terminated early | Log ends mid-task, exit code 0 | Agent used Skill tool (terminates `claude -p` sessions) |
| Permission denied | Tool call rejected in log | Missing `--allowedTools` entry |
| Auth error | `401 Unauthorized` in tool output | `.env` not sourced before `claude -p` |
| Sandbox block | `Operation not permitted` | Shell substitution hit sandbox — use `python3 -c` workaround |
| Budget exhausted | `max_turns reached` in log | Increase `--max-turns` or simplify the task |

### Reading headless audit logs

```bash
# Find latest headless log
ls -t .claude/autonomous-sessions/headless-*.log | head -1

# Search for errors
grep -i 'error\|failed\|denied\|blocked' <log-file>

# Count tool calls (measure efficiency)
grep -c 'tool_use' <log-file>
```

## Interpreting Event Metrics

### Event analysis patterns

| Pattern | What it indicates | When to check |
|---------|------------------|---------------|
| High bash_command count relative to file_write | Excessive iteration — agent may be stuck | After long autonomous runs |
| Repeated failed tool calls | Permission or configuration issue | After headless failures |
| No events logged | Event logging hook may not be registered | After any session with no events.jsonl |
| Spike in safety events | Injection patterns or false positives | After suspicious behavior |

### Eval regression detection

Compare current session metrics against a saved baseline. A >10% drop in any metric category warrants investigation. Common causes:
- Hooks that reject previously-allowed operations
- CLAUDE.md changes that alter agent behavior
- Dependency updates that break tests

## Diagnosing Hook Issues

### Which hook blocked?

All hooks emit stderr with `[hook-name]` prefix. Check:

```bash
# Pre-write blocks
# Look for: credential patterns, ABOUTME missing, meta-mode isolation, naming conventions

# Pre-bash blocks
# Look for: --no-verify, force push, deploy without tests, pending-actions

# Post-bash warnings
# Look for: value leakage detection, deploy completion, test completion
```

### Hook override environment variables

| Variable | Overrides |
|----------|-----------|
| `SKIP_PIPELINE_GATE=true` | New file without tests gate |
| `SKIP_PENDING_ACTIONS=true` | Pending actions commit block |
| `SKIP_TSC_CHECK=true` | TypeScript compilation check |
| `SKIP_META_HOOK_CHECK=true` | Meta-only hook registration block |
| `CLAUDE_ALLOW_PRODUCTION_WRITE=true` | Meta-mode write isolation |

### Hook dependencies

All safety hooks require `jq`. Without it, hooks silently skip ALL validation. Verify:

```bash
which jq || echo "CRITICAL: jq not installed — hooks are not enforcing anything"
```

## Cross-References

- Headless gotchas: Check autonomous session documentation
- Hook source: `.claude/hooks/` directory
- Operational gotchas: `.claude/references/operational-gotchas.md`
