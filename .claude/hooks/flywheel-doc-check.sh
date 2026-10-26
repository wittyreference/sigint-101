#!/bin/bash
# ABOUTME: Documentation flywheel hook - suggests doc updates from multiple sources.
# ABOUTME: Environment-aware: writes to .meta/ (meta) or .claude/ (shipped).

# This hook combines FOUR sources for doc suggestions:
# 1. Uncommitted files (git status) - catches pre-commit needs
# 2. Recent commits (since session start) - catches post-commit needs
# 3. Session-tracked files (from post-write hook) - catches everything touched
# 4. Validation failures (from PatternTracker) - catches unresolved issues

# Parse hook input for session_id (PostToolUse passes JSON on stdin)
HOOK_INPUT=""
if [ ! -t 0 ]; then
    HOOK_INPUT="$(cat)"
fi
HOOK_SESSION_ID=""
if [ -n "$HOOK_INPUT" ] && command -v jq &> /dev/null; then
    HOOK_SESSION_ID="$(echo "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source environment detection helper
source "$SCRIPT_DIR/_meta-mode.sh"

# Source config reader for doc mappings
if [ -f "$SCRIPT_DIR/_config-reader.sh" ]; then
    source "$SCRIPT_DIR/_config-reader.sh"
fi

# Use environment-aware paths from _meta-mode.sh
PENDING_ACTIONS_FILE="$CLAUDE_PENDING_ACTIONS"
SESSION_DIR="$(dirname "$CLAUDE_PENDING_ACTIONS")"

# Session-scoped state: read from per-session files for concurrent session support
if [ -n "$HOOK_SESSION_ID" ]; then
    SESSIONS_DIR="$SESSION_DIR/.sessions"
    SESSION_FILE="$SESSIONS_DIR/${HOOK_SESSION_ID}.files"
    SESSION_START_FILE="$SESSIONS_DIR/${HOOK_SESSION_ID}.start"
    LAST_RUN_FILE="$SESSIONS_DIR/${HOOK_SESSION_ID}.doc-check"
else
    # Fallback for missing session_id
    SESSION_FILE="$SESSION_DIR/.session-files"
    SESSION_START_FILE="$SESSION_DIR/.session-start"
    LAST_RUN_FILE="$SESSION_DIR/.last-doc-check"
fi

# Accept --force flag to skip debounce
FORCE=false
if [ "$1" = "--force" ]; then
    FORCE=true
fi

# Debounce: Only run if 2+ minutes have passed since last check
if [ "$FORCE" = false ] && [ -f "$LAST_RUN_FILE" ]; then
    LAST_RUN=$(cat "$LAST_RUN_FILE" 2>/dev/null | head -1)
    NOW=$(date +%s)
    DIFF=$((NOW - LAST_RUN))
    if [ "$DIFF" -lt 120 ]; then
        exit 0
    fi
fi

# Update last run timestamp
date +%s > "$LAST_RUN_FILE"

# Get timestamp for display
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

# Check if we're in a git repo
cd "$PROJECT_ROOT" || exit 0
if [ ! -d ".git" ]; then
    exit 0
fi

# ============================================
# COLLECT FILES FROM ALL THREE SOURCES
# ============================================

ALL_FILES=""

# Source 1: Uncommitted files (staged, modified, untracked)
UNCOMMITTED=$(git status --porcelain 2>/dev/null | grep -E "^(M|A| M| A|\?\?)" | awk '{print $NF}')
if [ -n "$UNCOMMITTED" ]; then
    ALL_FILES="$ALL_FILES"$'\n'"$UNCOMMITTED"
fi

# Source 2: Files from recent commits (since session start)
if [ -f "$SESSION_START_FILE" ]; then
    SESSION_START=$(cat "$SESSION_START_FILE")
    # Get files changed in commits since session start
    COMMITTED_FILES=$(git log --since="@$SESSION_START" --name-only --pretty=format: 2>/dev/null | grep -v "^$" | sort -u)
    if [ -n "$COMMITTED_FILES" ]; then
        ALL_FILES="$ALL_FILES"$'\n'"$COMMITTED_FILES"
    fi
fi

# Source 3: Session-tracked files (from post-write hook)
if [ -f "$SESSION_FILE" ]; then
    # Extract just the file paths (format is timestamp|filepath)
    SESSION_TRACKED=$(cut -d'|' -f2 "$SESSION_FILE" 2>/dev/null)
    if [ -n "$SESSION_TRACKED" ]; then
        ALL_FILES="$ALL_FILES"$'\n'"$SESSION_TRACKED"
    fi
fi

# Source 4: Validation failure patterns (from PatternTracker)
# Check for pattern database in both .meta and .claude locations
PATTERN_DB_META="$PROJECT_ROOT/.meta/pattern-db.json"
PATTERN_DB_CLAUDE="$PROJECT_ROOT/.claude/pattern-db.json"
PATTERN_DB=""
if [ -f "$PATTERN_DB_META" ]; then
    PATTERN_DB="$PATTERN_DB_META"
elif [ -f "$PATTERN_DB_CLAUDE" ]; then
    PATTERN_DB="$PATTERN_DB_CLAUDE"
fi

# Track unresolved pattern count for later use
UNRESOLVED_PATTERNS=0
PATTERN_CATEGORIES=""
if [ -n "$PATTERN_DB" ] && command -v jq &> /dev/null; then
    UNRESOLVED_PATTERNS=$(jq '[.patterns | to_entries[] | select(.value.resolved == false)] | length' "$PATTERN_DB" 2>/dev/null || echo "0")
    if [ "$UNRESOLVED_PATTERNS" -gt 0 ]; then
        PATTERN_CATEGORIES=$(jq -r '[.patterns | to_entries[] | select(.value.resolved == false) | .value.category] | unique | join(", ")' "$PATTERN_DB" 2>/dev/null || echo "")
    fi
fi

# Source 5: CLAUDE.md inventory drift
DRIFT_SCRIPT="$PROJECT_ROOT/scripts/check-claude-doc-drift.sh"
if [ -x "$DRIFT_SCRIPT" ]; then
    DRIFT_OUTPUT=$("$DRIFT_SCRIPT" --quiet 2>/dev/null || true)
    if [ -n "$DRIFT_OUTPUT" ]; then
        ALL_FILES="$ALL_FILES"$'\n'"$DRIFT_OUTPUT"
    fi
fi

# Deduplicate, clean up, and exclude flywheel's own output files
ALL_FILES=$(echo "$ALL_FILES" | grep -v "^$" | grep -v "pending-actions\.\(md\|json\)" | grep -v "\.session-files" | grep -v "\.session-start" | grep -v "\.last-doc-check" | grep -v "\.sessions/" | sort -u)

if [ -z "$ALL_FILES" ]; then
    exit 0
fi

# ============================================
# GENERATE DOC SUGGESTIONS (CONFIG-DRIVEN)
# ============================================

SUGGESTIONS=""   # blocking: each one states a fact that can be made false
ADVISORIES=""    # non-blocking: worth a look, but nothing here is checkable

# Findings that BLOCK a commit must describe what is being committed. Judging every
# uncommitted file meant a docs-only commit could be refused over unrelated work in
# progress — the same "blocks the present for something not landing" failure that
# accumulating findings caused. Advisories still consider everything.
STAGED_FILES=$(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null)
BLOCKING_SCOPE="${STAGED_FILES:-}"

# Did this change touch a given path? Advisory scope: everything uncommitted.
touched() { echo "$ALL_FILES" | grep -qF "$1"; }

# Is this path part of what is actually being committed?
staged() { [ -n "$BLOCKING_SCOPE" ] && echo "$BLOCKING_SCOPE" | grep -qF "$1"; }

# Config-driven doc mappings. A mapping is only a finding when the source changed
# and its documentation did NOT change alongside it. "Source changed, so review the
# doc" is unfalsifiable — you can never satisfy it — which is what made this gate
# block forever and train everyone to reach for the override.
while IFS=$'\t' read -r SOURCE_PATTERN DOC_TARGET; do
    [ -z "$SOURCE_PATTERN" ] && continue
    if staged "$SOURCE_PATTERN" && ! touched "$DOC_TARGET"; then
        SUGGESTIONS="${SUGGESTIONS}• ${SOURCE_PATTERN} changed but ${DOC_TARGET} did not\n"
    fi
done < <(ff_doc_mappings)

# Tracked source changed without the todo being updated.
TRACKED_DIRS=$(ff_config_array ".trackedDirectories" 2>/dev/null)
TODO_FILE="todo.md"
[ "$CLAUDE_META_MODE" = "true" ] && TODO_FILE=".meta/todo.md"
if [ -n "$TRACKED_DIRS" ] && ! touched "$TODO_FILE"; then
    while IFS= read -r dir; do
        if [ -n "$dir" ] && echo "$BLOCKING_SCOPE" | grep -q "^${dir}"; then
            SUGGESTIONS="${SUGGESTIONS}• ${dir} changed but ${TODO_FILE} did not\n"
            break
        fi
    done <<< "$TRACKED_DIRS"
fi

# New env vars without the example file following.
if staged ".env" && ! touched ".env.example"; then
    SUGGESTIONS="${SUGGESTIONS}• .env changed but .env.example did not\n"
fi

# A real count of unresolved validation failures is evidence, not a guess.
if [ "$UNRESOLVED_PATTERNS" -gt 0 ]; then
    SUGGESTIONS="${SUGGESTIONS}• $UNRESOLVED_PATTERNS unresolved validation patterns"
    if [ -n "$PATTERN_CATEGORIES" ]; then
        SUGGESTIONS="${SUGGESTIONS} (categories: $PATTERN_CATEGORIES)"
    fi
    SUGGESTIONS="${SUGGESTIONS}\n"
fi

# --- Advisory only. These are prompts to think, and a prompt to think must never
# --- be a gate: there is no edit that makes "if behaviour changed" stop being true.
touched ".claude/hooks" && ADVISORIES="${ADVISORIES}• CLAUDE.md hooks section, if hook behaviour changed\n"
touched ".claude/references" && ADVISORIES="${ADVISORIES}• doc-map.md, if it should point at updated references\n"
if echo "$ALL_FILES" | grep -qE "(types|config)\.(ts|js|json)$"; then
    ADVISORIES="${ADVISORIES}• CLAUDE.md, if interfaces or config options changed\n"
fi
if echo "$ALL_FILES" | grep -qE "(__tests__|test/|tests/|test_|\.test\.|\.spec\.)"; then
    ADVISORIES="${ADVISORIES}• CLAUDE.md, if new test patterns were established\n"
fi
NEW_FILES_COUNT=$(echo "$ALL_FILES" | wc -l | tr -d ' ')
if [ "$NEW_FILES_COUNT" -gt 3 ]; then
    ADVISORIES="${ADVISORIES}• DESIGN_DECISIONS.md, if an architectural decision was made\n"
fi

# ============================================
# WRITE SUGGESTIONS TO PENDING-ACTIONS
# ============================================

if [ -n "$SUGGESTIONS" ]; then
    # Rewritten every run, never appended to. A finding states a fact that was true
    # when it was written; kept around, it becomes a claim about the past that blocks
    # work in the present. The previous append-with-24h-dedup meant "tests/ changed
    # but todo.md did not" kept blocking after todo.md had been updated — the same
    # unfalsifiable trap in a slower form.
    cat > "$PENDING_ACTIONS_FILE" << 'EOF'
# Pending Documentation Actions

Findings from the documentation flywheel, re-evaluated on every run. Each one names a
change that landed without its documentation following. Make the statement false and it
disappears — there is nothing here to dismiss.

---

EOF

    UNCOMMITTED_COUNT=$(echo "$UNCOMMITTED" | grep -c "." 2>/dev/null | tr -d '\n' || echo "0")
    COMMITTED_COUNT=$(echo "$COMMITTED_FILES" | grep -c "." 2>/dev/null | tr -d '\n' || echo "0")
    SESSION_COUNT=$(echo "$SESSION_TRACKED" | grep -c "." 2>/dev/null | tr -d '\n' || echo "0")

    echo -e "$SUGGESTIONS" | while IFS= read -r line; do
        [ -n "$line" ] && echo "- [$TIMESTAMP] $line" >> "$PENDING_ACTIONS_FILE"
    done

    # Output summary to stderr (visible in hook output)
    TOTAL_FILES=$(echo "$ALL_FILES" | wc -l | tr -d ' ')
    SUMMARY="Doc flywheel: Analyzed $TOTAL_FILES files (uncommitted:$UNCOMMITTED_COUNT, committed:$COMMITTED_COUNT, session:$SESSION_COUNT"
    if [ "$UNRESOLVED_PATTERNS" -gt 0 ]; then
        SUMMARY="$SUMMARY, validation-failures:$UNRESOLVED_PATTERNS"
    fi
    SUMMARY="$SUMMARY)"
    echo "$SUMMARY" >&2
fi

if [ -z "$SUGGESTIONS" ] && [ -f "$PENDING_ACTIONS_FILE" ]; then
    rm -f "$PENDING_ACTIONS_FILE"
fi

if [ -n "$ADVISORIES" ]; then
    echo "" >&2
    echo "Worth a look (advisory — never blocks):" >&2
    echo -e "$ADVISORIES" | while IFS= read -r line; do
        [ -n "$line" ] && echo "  $line" >&2
    done
fi

exit 0
