#!/bin/bash
# ABOUTME: Shared environment detection helper for Claude Code hooks.
# ABOUTME: Source this file to detect if we're in meta-development mode.

# Get project root (caller can override if already set)
export PROJECT_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

# Detect meta-development mode
if [[ -d "$PROJECT_ROOT/.meta" ]]; then
    export CLAUDE_META_MODE="true"
    export CLAUDE_META_DIR="$PROJECT_ROOT/.meta"
    export CLAUDE_PENDING_ACTIONS="$PROJECT_ROOT/.meta/pending-actions.md"
    export CLAUDE_LEARNINGS="$PROJECT_ROOT/.meta/learnings.md"
    export CLAUDE_LOGS_DIR="$PROJECT_ROOT/.meta/logs"
    export CLAUDE_PLANS_DIR="$PROJECT_ROOT/.meta/plans"
    export CLAUDE_LEARNING_DIR="$PROJECT_ROOT/.meta/learning"
else
    export CLAUDE_META_MODE="false"
    export CLAUDE_META_DIR=""
    export CLAUDE_PENDING_ACTIONS="$PROJECT_ROOT/.claude/pending-actions.md"
    export CLAUDE_LEARNINGS="$PROJECT_ROOT/.claude/learnings.md"
    export CLAUDE_LOGS_DIR="$PROJECT_ROOT/.claude/logs"
    export CLAUDE_PLANS_DIR="$PROJECT_ROOT/.claude/archive/plans"
    export CLAUDE_LEARNING_DIR=""
fi

# Helper function for conditional meta-mode execution
# Usage: run_if_meta "command" "args..."
run_if_meta() {
    if [[ "$CLAUDE_META_MODE" == "true" ]]; then
        "$@"
    fi
}

# Helper function to get the appropriate directory
# Usage: get_claude_dir "logs" -> returns appropriate logs directory
get_claude_dir() {
    local type="$1"
    case "$type" in
        logs) echo "$CLAUDE_LOGS_DIR" ;;
        plans) echo "$CLAUDE_PLANS_DIR" ;;
        pending) echo "$(dirname "$CLAUDE_PENDING_ACTIONS")" ;;
        *) echo "$PROJECT_ROOT/.claude" ;;
    esac
}
