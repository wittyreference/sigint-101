---
description: Log a knowledge miss event for RAG adoption data collection. Use when Claude should have known something but didn't.
allowed-tools: Bash, Read, Grep
argument-hint: [description of what was missed]
---

# Knowledge Miss Logger

Log a knowledge miss event — when Claude should have had context from prior work but didn't.

## Input

<user_request>
$ARGUMENTS
</user_request>

## Behavior

1. **Classify the miss** into one of these categories:
   - `semantic_gap` — Content exists in the codebase but keyword search wouldn't find it (e.g., searched for "auth" but answer was in a file about "identity verification")
   - `not_in_memory` — Content was never captured in memory files or docs
   - `cross_repo` — Content exists in a sibling repo (sibling repositories)
   - `stale` — A memory entry exists but is outdated or wrong
   - `plan_archaeology` — The answer was in a plan file but the plan wasn't discoverable

2. **Identify the resolution** — briefly note where the information was eventually found (or if it wasn't found at all).

3. **Emit the event** by running this bash command (substitute the actual values):

```bash
source .claude/hooks/_emit-event.sh
EMIT_SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"
emit_event "knowledge_miss" "$(jq -nc \
  --arg desc 'DESCRIPTION' \
  --arg cat 'CATEGORY' \
  --arg res 'RESOLUTION' \
  '{description: $desc, category: $cat, resolution: $res}')"
```

4. **Confirm** to the user: "Knowledge miss logged: [category] — [description]"

## Query

To review collected data, run:
```bash
./scripts/query-events.sh knowledge
```

## Purpose

This data feeds the 90-day RAG adoption decision. If knowledge misses average >2/session with >50% being `semantic_gap`, the project will adopt vector search. If <1/session, file-based improvements are sufficient.
