---
name: memory-systems
description: State tracking and session memory management. Use when implementing cross-session persistence, managing application state, or designing memory patterns for development.
---

# Memory Systems for Feature Development

This skill covers state tracking and memory management for development sessions.

## Memory Types

| Memory Type | Scope | Duration | Storage |
|-------------|-------|----------|---------|
| Session | Current conversation | Until session ends | In-context |
| Project | Across sessions | Persistent | Files (CLAUDE.md, git) |
| Workflow | Per feature/task | Until task complete | Todo list, commits |

## Session Memory (In-Context)

Track progress within the current Claude Code session.

### What to Track

- Current task/feature being built
- Files created or modified
- Tests written and their status
- Key decisions made
- Blockers encountered

### Session Summary Format

Maintain a mental summary after each major action:

```markdown
## Session: User Registration Feature

### Completed
- [x] Created src/handlers/register.js
- [x] Added input validation with email/password rules
- [x] Unit tests passing (4/4)

### In Progress
- [ ] Integration test for full flow

### Decisions Made
- Using bcrypt for password hashing
- Email uniqueness enforced at DB level
- Return 409 for duplicate registrations

### Files Modified
- src/handlers/register.js (new)
- src/middleware/validate.js (new)
- __tests__/unit/handlers/register.test.js (new)

### Next Steps
- Write integration test
- Run /review for approval
```

### When to Update Session Memory

- After completing a file
- After tests pass/fail
- After making an architectural decision
- Before switching to a different task
- When resuming after interruption

## Project Memory (Persistent)

Information that persists across sessions via files.

### CLAUDE.md as Project Memory

Root CLAUDE.md stores:
- Project standards and conventions
- Available commands and workflows
- Environment variable requirements
- Testing requirements

Subdirectory CLAUDE.md stores:
- Domain-specific patterns
- API usage examples
- Configuration references
- Common error codes

### Git History as Memory

```bash
# Recent decisions
git log --oneline -10

# What changed for a feature
git log --oneline --grep="user registration"

# Files touched recently
git diff --name-only HEAD~5
```

### Todo.md as Task Memory

```markdown
# Project Todo

## In Progress
- [ ] User registration API

## Pending
- [ ] Password reset flow
- [ ] OAuth integration

## Completed
- [x] Initial project setup
- [x] Database schema
- [x] Health check endpoint
```

### Design Decisions as Architectural Memory

```markdown
# DESIGN_DECISIONS.md

## Authentication Strategy
- Decision: JWT with refresh tokens
- Rationale: Stateless auth scales horizontally
- Date: 2025-06-15
- Alternatives considered: Session-based, API keys

## Database Choice
- Decision: PostgreSQL with Prisma ORM
- Rationale: Strong typing, migration support
- Date: 2025-06-10
```

## Workflow Memory

Track progress through development workflows.

### Git as Source of Truth

Git history is the primary activity log:
- Commits capture what changed and why
- `todo.md` captures session progress
- `learnings.md` captures discoveries (capture -> promote -> clear)
- `DESIGN_DECISIONS.md` captures architectural rationale

```bash
# What was done recently
git log --oneline -10

# What files changed for a feature
git log --name-only --oneline --grep="user registration"

# Diff since last tag/checkpoint
git diff HEAD~5 --name-only
```

### Documentation Flywheel

The documentation flywheel uses file-based communication:
- Hooks write suggestions to `.claude/pending-actions.md`
- Agent reads file before commits
- File is cleared after actions addressed

```
Hook runs -> pending-actions.md -> Agent reads -> Takes action -> Clears file
```

### Workflow State Tracking

For multi-phase pipeline workflows, track phase completion:

```markdown
## Workflow: new-feature "User Registration"

Status: IN_PROGRESS

### Phase History
| Phase | Agent | Status | Timestamp |
|-------|-------|--------|-----------|
| 1 | /architect | COMPLETE | 10:23 |
| 2 | /spec | COMPLETE | 10:35 |
| 3 | /test-gen | COMPLETE | 10:52 |
| 4 | /dev | IN_PROGRESS | 11:05 |

### Artifacts Created
- spec.md (phase 2)
- __tests__/unit/handlers/register.test.js (phase 3)
- src/handlers/register.js (phase 4, in progress)

### Blockers
- None

### Decisions Log
- Phase 1: Chose REST pattern with validation middleware
- Phase 2: Input validation with Joi schema
- Phase 3: 6 test cases defined
```

## State Management Patterns

### Pattern 1: In-Context Working Memory

Keep current task state in the conversation context:

```markdown
Current state:
- Building: user registration endpoint
- Phase: implementation (tests written, making them pass)
- Blocked: no
- Files open: register.js, register.test.js
- Last action: fixed email validation regex
```

### Pattern 2: File-Based State

Use project files for state that needs to persist between sessions:

```
CLAUDE.md          -> Project standards (permanent)
DESIGN_DECISIONS.md -> Architecture choices (permanent)
todo.md            -> Task progress (semi-permanent)
learnings.md       -> Discoveries (temporary, promote or clear)
pending-actions.md -> Hook suggestions (ephemeral, clear after addressing)
```

### Pattern 3: Git-Based State

Use git for tracking what changed and why:

```bash
# Current state
git status

# Recent activity
git log --oneline -5

# What's different from main
git diff main --name-only
```

### State Logging for Debugging

Log state transitions for debugging:

```javascript
// ABOUTME: Logs state transitions for debugging.
// ABOUTME: Append-only log pattern for flow tracing.

const logStateTransition = (sessionId, fromState, toState, data) => {
  console.log(JSON.stringify({
    timestamp: new Date().toISOString(),
    sessionId,
    transition: `${fromState} -> ${toState}`,
    data
  }));
};

// Usage
logStateTransition('session_123', 'validating', 'processing', { userId: 'usr_abc' });
```

## Memory Anti-Patterns

### Don't Store

- Full API responses (too large)
- Passing test output (no longer relevant)
- Resolved error messages
- Old file versions (git handles this)

### Don't Repeat

- Information already in CLAUDE.md
- Standard API signatures
- Boilerplate patterns
- Previously stated decisions

### Don't Persist

- Session-specific debugging info
- Temporary workarounds
- One-time configuration steps

## Memory Retrieval Patterns

### "What did we decide about X?"

```bash
# Search git commits
git log --all --oneline --grep="X"

# Search CLAUDE.md files
grep -r "X" --include="CLAUDE.md" .
```

### "What files did we change for feature Y?"

```bash
# If committed
git log --name-only --oneline --grep="Y"

# If in progress
git status
```

### "What was the error we fixed?"

```bash
# Check recent test changes
git diff HEAD~3 __tests__/

# Check application logs
cat logs/error.log | tail -20
```

## Implementation Checklist

For new features, track:

- [ ] Requirements understood
- [ ] Architecture decided
- [ ] Tests written (failing)
- [ ] Implementation complete (tests passing)
- [ ] Code reviewed
- [ ] Documentation updated
- [ ] Committed to git

This checklist serves as memory of what's done and what remains.
