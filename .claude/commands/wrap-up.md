---
description: End-of-session cleanup and documentation. Use when wrapping up, ending a session, or capturing learnings and pending actions before stopping.
---

# Session Wrap-Up

Review the current session's work and update all relevant documentation before committing.

## Steps

### 1. Gather Session Context

Determine environment:
- If `.meta/` exists → meta-development mode (learnings: `.meta/learnings.md`, pending: `.meta/pending-actions.json`, todo: `.meta/todo.md`)
- Otherwise → standard mode (learnings: `.claude/learnings.md`, pending: `.claude/pending-actions.json`, todo: `todo.md`)

Collect what changed this session:
```bash
git diff --name-only HEAD  # unstaged + staged changes vs last commit
git diff --cached --name-only  # staged changes only
```

Read the pending actions file for flywheel-generated suggestions.

### 1b. Mine Commit Messages for Un-Captured Learnings

Check this session's commits for discovery signals that may not have been recorded:

```bash
# Get session start timestamp (check per-session file first, then legacy shared file)
SESSION_DIR={session-dir}
SESSION_START=$(ls -t "$SESSION_DIR"/.sessions/*.start 2>/dev/null | head -1 | xargs cat 2>/dev/null)
if [ -z "$SESSION_START" ]; then
    SESSION_START=$(cat "$SESSION_DIR/.session-start" 2>/dev/null)
fi
# List commits made this session
git log --since="@${SESSION_START}" --format='%h %s' 2>/dev/null
```

Scan commit subjects for learning signal words: `fix`, `discover`, `gotcha`, `quirk`, `workaround`, `bug`, `edge case`, `actually`, `found that`, `regression`, `broken`, `issue`.

For each flagged commit:
1. Check if the topic already appears in the learnings file
2. If not, flag it as a potential un-captured learning

For each flagged commit:
1. Check if the topic already appears in the learnings file
2. If not, flag it as a potential un-captured learning
3. **Auto-detect knowledge misses**: If a `fix:` or `workaround:` commit addresses something that should have been documented but wasn't, auto-emit a knowledge miss:
   ```bash
   source .claude/hooks/_emit-event.sh
   EMIT_SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"
   emit_event "knowledge_miss" "$(jq -nc --arg desc 'Fix for undocumented issue: COMMIT_SUBJECT' --arg cat 'not_in_memory' --arg res 'Now captured in learnings' '{description: $desc, category: $cat, resolution: $res, phase: "wrap-up"}')"
   ```

Report flagged commits (if any) before proceeding to step 2:
```
Potential un-captured learnings from commits:
- abc1234: "fix: Something unexpected" — not found in learnings
```

Skip this step if no commits were made this session or none match signal words.

### 2. Capture Learnings

Review the session's changes and identify anything worth recording:
- Debugging insights or root causes discovered
- API quirks or gotchas encountered
- Patterns that worked (or didn't)
- Configuration pitfalls

Add entries to the learnings file using the standard format:
```markdown
## [YYYY-MM-DD] Topic

**Discoveries:**

1. **Finding**: What was learned
   - Context and details
```

If a learning is stable and broadly applicable, promote it directly to the target doc (CLAUDE.md, DESIGN_DECISIONS.md, hooks-reference, etc.) and note "Promoted to: [target]" in the learnings entry.

### 2b. Learnings Archival (if clearing)

If learnings.md will be cleared (partially or fully):

1. **First**: Append entries being removed to `learnings-archive.md` (same directory, insert below header, above existing entries)
2. **Then**: Remove them from `learnings.md`
3. The pre-write hook blocks bulk clears that skip the archive step

### 3. Update Documentation

For each changed file, determine if documentation needs updating:

| Changed Area | Check These Docs |
|--------------|------------------|
| `.claude/hooks/` | `.claude/skills/hooks-reference.md`, root CLAUDE.md |
| Source code directories | Relevant subdirectory CLAUDE.md |
| `scripts/` | Scripts documentation |
| Architecture changes | `DESIGN_DECISIONS.md` |
| New slash commands or skills | Root CLAUDE.md slash command table |
| New invariants | Root CLAUDE.md "Architectural Invariants" section |
| New CLAUDE.md or REFERENCE.md files | `.claude/references/doc-map.md` |

Only update docs where the session's changes actually warrant it. Don't touch docs for unrelated areas.

### 3b. Promotion Proposals (Learning + Gotcha)

Check `pending-actions.json` for promotion proposals:

```bash
jq '[.[] | select(.type == "learning-promotion" or .type == "gotcha-promotion")]' .meta/pending-actions.json 2>/dev/null || echo "No promotion proposals"
```

**For `learning-promotion` entries**:
1. Read the `proposedContent` and `targetFile`
2. Verify the content is accurate and worth promoting (not a one-off or misleading)
3. If approved: add the content to the target CLAUDE.md file in the appropriate section (usually Gotchas or Troubleshooting)
4. Mark as promoted: the LearningCaptureEngine tracks this via pattern ID
5. If rejected: remove the entry from pending-actions.json

Learning promotion criteria (automated pipeline already filters, but verify):
- Pattern seen 3+ times across 2+ sessions
- Pattern was resolved (a fix worked)
- Content isn't already in the target file

**For `gotcha-promotion` entries** (generated by validation runs):

These are recurring validation findings that have appeared in 2+ runs and should be codified into domain docs to prevent future recurrence.

1. Read the `gotchaText`, `recommendation`, `targetSkillFile`, and `targetSection`
2. Read the target file and find its Gotchas / Edge Cases section
3. Verify the gotcha is:
   - **Not already present**: Check for semantic overlap with existing gotchas
   - **Accurate and actionable**: The finding describes a real behavior or constraint
   - **Correctly scoped**: The gotcha belongs in this specific file, not cross-cutting docs
4. If approved: add the gotcha to the target file's Gotchas section
5. If rejected: remove the entry from pending-actions.json

### 4. Sync Auto-Memory and Shipped Docs

**Promote outward**: Check auto-memory for entries that should be in shipped docs:

| Entry Type | Promote To |
|------------|------------|
| API/SDK gotcha (clear domain) | Domain CLAUDE.md Gotchas section |
| Cross-cutting gotcha | `.claude/references/operational-gotchas.md` |
| High-impact rule | Root CLAUDE.md |
| Architectural decision (why X over Y) | `DESIGN_DECISIONS.md` |
| Per-developer convention | Keep in auto-memory |

After promoting, replace the detailed item with a pointer (e.g., "See [domain]/CLAUDE.md#gotchas"). Don't delete — pointers prevent re-discovery of the same gotcha.

**Tag stale entries for auto-removal**: Entries that meet ANY of these criteria should be tagged with `<!-- prune -->` above their `##` header:

- **Session implementation history**: Sections with "(Session N)" that document WHAT was built, not operational patterns needed going forward. The scripts/code they describe are self-documenting.
- **Duplicate of shipped docs**: Content that exists word-for-word in root CLAUDE.md, a domain CLAUDE.md, or a references file. Replace with a one-line pointer before tagging.
- **Obsolete pointers**: References to plans, roadmaps, or state files that no longer exist.

Tagged entries are auto-removed at next session start by `session-start-log.sh`.

Example:
```markdown
<!-- prune -->
## Some Stale Section (Session 42)
- Details that are now in shipped docs...
```

**Cross-check learnings and auto-memory**: Ensure nothing fell through the cracks:
- Read the session learnings file — are there entries that should also be in auto-memory (for cross-session persistence)?
- Read auto-memory — are there entries from this session that should also be in the learnings file (for the promote/clear flywheel)?
- Are there auto-memory entries that represent an architectural choice worth recording in `DESIGN_DECISIONS.md`? Signs: "we chose X over Y", rationale for defaults, explicit opt-in requirements.

**Capture inward**: Add session learnings that should persist across sessions to auto-memory.

### 5. Update Todo

Read the todo file and reconcile it against this session's actual work.

**Determine todo path:**
- If `.meta/` exists → `.meta/todo.md`
- Otherwise → `todo.md`

**Cross-reference session work against todo:**

1. Read the full todo file
2. Gather what was done this session:
   ```bash
   # Files changed
   git diff --name-only HEAD
   # Commits made
   SESSION_DIR={session-dir}
   SESSION_START=$(ls -t "$SESSION_DIR"/.sessions/*.start 2>/dev/null | head -1 | xargs cat 2>/dev/null)
   if [ -z "$SESSION_START" ]; then
       SESSION_START=$(cat "$SESSION_DIR/.session-start" 2>/dev/null)
   fi
   git log --since="@${SESSION_START}" --format='%h %s' 2>/dev/null
   ```
3. For each todo section, check if this session's work touches it:
   - **Checkboxes completed** → tick them: `- [ ]` → `- [x]`
   - **Section fully done** → change header from `IN PROGRESS` to `DONE`, add completion date
   - **Partial progress** → add a note under the section (e.g., "Step 2 of 5 done — deployed but not yet wired")
   - **Key numbers changed** → update counts, dates in the "Key Numbers" block
   - **New blockers discovered** → add them as unchecked items in the relevant section
   - **New work items discovered** → add to the appropriate tier (don't create a new section if it fits an existing one)

**What NOT to do:**
- Don't reorganize or reformat sections you didn't touch
- Don't move items between tiers unless the session's work changed the effort/ROI assessment
- Don't remove DONE sections (they serve as history until the next archival pass)
- Don't update items unrelated to this session's work

**Report in the summary** (step 10): list every checkbox ticked, status changed, or item added, so the user can verify.

### 5b. Generate Learning Exercises

Generate learning exercises from two sources:

**Code exercises** (from autonomous work):
1. Check if `.meta/learning/session-log.jsonl` has events
2. If so, run the exercise generation: `bash .claude/hooks/generate-learning-exercises.sh`
3. Report how many code exercises were generated

**Knowledge exercises** (from project evolution):
The generation hook also scans for new design decisions, learnings, and validation reports that appeared since the last generation run (tracked in `.meta/learning/generation-state.json`). These produce decision deep-dive, gotcha prediction, and diagnosis exercises.

4. Report total exercises generated (code + knowledge) for the next interactive session
5. Remind: "Use `/learn status` to see coverage across all knowledge areas"

### 6. Context Budget Check

Quick health check on auto-loaded context size:

```bash
MEMORY_PATH="$HOME/.claude/projects/$(pwd | sed 's|/|-|g')/memory/MEMORY.md"
wc -l CLAUDE.md "$MEMORY_PATH" 2>/dev/null
```

Report the MEMORY.md line count in the summary. If over 150 lines, flag it — entries beyond 200 are truncated and never seen. Prune by replacing promoted entries with pointers.

### 7. Infrastructure Health Checks

Quick checks that hooks and automation are still working:

**Compaction summary capture:**

With 1M context windows, compaction is rare. Only check this if compaction actually occurred during the session:
```bash
# Check if this session had any compaction events
grep "$(date +%Y-%m-%d)" .meta/logs/session-events.log 2>/dev/null | grep -q "source=compact"
```
If compaction fired this session, verify a corresponding summary was captured:
```bash
ls -lt .meta/logs/compaction-summary-*.md 2>/dev/null | head -1
```
If compaction occurred but no summary exists, the `post-compact-summary.sh` hook may have stopped firing. Skip silently if no compaction occurred.

**Plan archiving:**
```bash
# Check the most recent archived plan
ls -lt .meta/plans/ 2>/dev/null | head -3
```
If plans are not being archived after plan mode exits, verify the `archive-plan.sh` hook is registered under `Stop` in `.claude/settings.json`.

Report any issues in the summary. Skip if both are healthy.

### 8. Session Observability & Evaluation

Analyze this session's structured event data for patterns and anomalies.

**Activity summary:**
```bash
./scripts/query-events.sh summary
```

Report the event counts. Flag anything unusual (e.g., high bash_command count relative to file_write may indicate excessive iteration).

**Safety check review:**
```bash
./scripts/query-events.sh safety
```

If any injection patterns were detected, report them with context. Were they false positives (legitimate code) or genuine concerns?

**Evaluation metrics:**
```bash
./scripts/eval-summary.sh today 2>/dev/null
```

Review the summary. If a baseline exists, run regression check.

Skip this section silently if events.jsonl doesn't exist (first session after setup).

### 8b. Knowledge Loop Closure

Check the health of the self-learning system and close any open loops:

**Knowledge miss summary:**
```bash
./scripts/query-events.sh knowledge
```
Report the count and top category. If misses average >2/session over the last 7 days, flag it — this is the threshold for reconsidering RAG adoption.

**Gotcha promotion backlog:**
Check for unreviewed gotcha-promotion candidates:
```bash
PENDING_JSON=".meta/pending-actions.json"
[ ! -f "$PENDING_JSON" ] && PENDING_JSON=".claude/pending-actions.json"
if [ -f "$PENDING_JSON" ]; then
    jq '[.[] | select(.type == "gotcha-promotion")] | length' "$PENDING_JSON" 2>/dev/null
fi
```
If gotcha-promotion entries exist, review them now (step 3b). If they've been pending for 2+ sessions, escalate.

**Plan index freshness:**
Verify the plan index was updated this session:
```bash
ls -la ~/.claude/plans/INDEX.md 2>/dev/null | head -1
```
If INDEX.md doesn't exist or is older than today, run: `bash .claude/hooks/index-plans.sh`

**Feedback loop health:**
Quick check — did this session's pipeline commands use prior knowledge?
- Check if any `knowledge_miss` events were emitted with `phase:` fields (indicates pipeline commands are logging gaps)
- If no phase-tagged events exist after a pipeline run, the prior knowledge checks may not be firing

### 9. Clear Pending Actions

After addressing flywheel suggestions, clear the pending actions file:
```markdown
# Pending Documentation Actions

Actions detected by the documentation flywheel. Review before committing.

---

<!-- Doc suggestions will be appended below this line by flywheel-doc-check.sh -->
```

### 10. Summary

Output what was updated:

```markdown
## Session Wrap-Up Complete

### Learnings Captured
- [list of entries added]

### Docs Updated
- [list of files modified with brief reason]

### Todo
- [items checked off or updated]

### Ready to Commit
[yes/no — and what to commit]
```

## Notes

- This is a review-and-update pass, not a rewrite. Make targeted edits.
- If nothing meaningful was learned or no docs need updating, say so — don't manufacture busywork.
- The user should review the changes before committing.

## Scope

<user_request>
$ARGUMENTS
</user_request>
</output>
