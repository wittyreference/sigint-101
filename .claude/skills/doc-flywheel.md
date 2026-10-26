---
name: doc-flywheel
description: Documentation-first knowledge management workflow. Use when capturing learnings, promoting discoveries to stable docs, or following the Check-Act-Record documentation protocol.
---

# Documentation Flywheel

Use the capture-promote-clear workflow for knowledge management.

## 1. Capture

Add discoveries to the learnings file (see Meta-Development Mode in CLAUDE.md for path):

- Type system gotchas or API quirks
- Debugging insights and root causes
- Patterns that work unexpectedly well
- Failed experiments worth remembering

## 2. Promote

Move stable learnings to permanent docs:

| Discovery Type | Target Doc |
|----------------|------------|
| Architectural choices | DESIGN_DECISIONS.md |
| New APIs or tools | API_REFERENCE.md |
| Tool/CLI boundaries | .claude/references/tool-boundaries.md |
| Session completion | Todo file session log |
| New patterns | Relevant CLAUDE.md files |

## 3. Clear

> **Enforced by hook**: The pre-write hook blocks bulk clearing of learnings.md
> if learnings-archive.md has not been updated in the same session.

Before removing promoted entries from learnings.md, **always copy them to learnings-archive.md first** (append at top, below header). Then remove from learnings.md. This preserves the full discovery history.

## Automation

The `doc-update-check.sh` hook detects file changes and appends documentation suggestions to the pending actions file. This file-based approach ensures reminders persist and are visible.

**Before committing, ALWAYS check for pending actions:**
```bash
jq -r '.actions[] | "- [\(.timestamp)] \(.target) - \(.reason)"' .meta/pending-actions.json 2>/dev/null || \
jq -r '.actions[] | "- [\(.timestamp)] \(.target) - \(.reason)"' .claude/pending-actions.json 2>/dev/null || \
echo "No pending actions"
```

After addressing actions, clear the file:
```bash
rm -f .meta/pending-actions.json .claude/pending-actions.json
```

## Documentation Standards for Technical Assertions

When writing documentation, technical assertions require verification to prevent misinformation.

### High-Risk Claims (MUST verify before writing)

- **Behavioral claims**: "X cannot/always/never does Y"
- **Hard limits**: Sizes, counts, timeouts, rate limits
- **Negative assertions**: "Not available", "Not supported", "Impossible"

### Verification Process

1. Search official docs for the relevant technology
2. If found: Add inline citation comment
3. If NOT found: Either don't make the claim, or mark as unverified

### Citation Format

```markdown
- Max payload 10MB <!-- verified: docs.example.com/limits -->
- Observed: X behavior <!-- UNVERIFIED: based on testing, needs official source -->
```

### Red Flags to Watch For

Words that indicate high-risk assertions requiring verification:
- "cannot", "can't", "not able to", "impossible"
- "always", "never", "must", "only"
- Specific numbers (limits, timeouts, counts) without source
