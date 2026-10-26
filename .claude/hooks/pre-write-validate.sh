#!/bin/bash
# ABOUTME: Pre-write validation hook for credential safety, ABOUTME, and meta isolation.
# ABOUTME: Blocks writes containing hardcoded credentials, missing headers, or violating meta mode.
#
# META MODE BYPASS: Use Bash with inline env var to write to production paths:
#   CLAUDE_ALLOW_PRODUCTION_WRITE=true cat > src/path/file.js << 'EOF'
#   ...
#   EOF

# Claude Code passes tool input as JSON on stdin, not env vars.
HOOK_INPUT=""
if [ ! -t 0 ]; then
    HOOK_INPUT="$(cat)"
fi

FILE_PATH=""
CONTENT=""
OLD_STRING=""
EMIT_SESSION_ID=""
# "1" when the payload carries a `content` key, which is how a Write is told from an Edit.
HAS_CONTENT_KEY="0"
if [ -n "$HOOK_INPUT" ] && ! command -v jq &> /dev/null; then
    echo "BLOCKED: jq not installed — safety hooks cannot run (credential detection, pipeline gate, ABOUTME). Install: brew install jq" >&2
    exit 2
fi
if [ -n "$HOOK_INPUT" ] && command -v jq &> /dev/null; then
    # One jq answers every question about the payload. Each spawn is ~100 ms on a Pi 4 and
    # this hook runs before every Write and Edit, so the four separate queries this used to
    # make were a third of the stall felt at the keyboard. The fields come back NUL-separated
    # because content is arbitrary text (newlines, quotes, backslashes) and a shell variable
    # cannot hold NUL anyway. Any NUL inside a field is dropped first, so a payload that
    # smuggles one cannot end the content early and hide what follows from the scans below.
    {
        IFS= read -r -d '' FILE_PATH
        IFS= read -r -d '' CONTENT
        IFS= read -r -d '' OLD_STRING
        IFS= read -r -d '' EMIT_SESSION_ID
        IFS= read -r -d '' HAS_CONTENT_KEY
    } < <(printf '%s' "$HOOK_INPUT" | jq -j '[
            (.tool_input.file_path // ""),
            (.tool_input.content // .tool_input.new_string // ""),
            (.tool_input.old_string // ""),
            (.session_id // ""),
            (if (.tool_input.content? // null) != null then "1" else "0" end)
        ] | map(tostring | gsub("\u0000"; "")) | join("\u0000")' 2>/dev/null; printf '\0')
fi

# Exit early if no content to validate
if [ -z "$CONTENT" ]; then
    exit 0
fi

# ============================================
# META-MODE ISOLATION CHECK
# ============================================

# Source meta-mode detection
HOOK_DIR="$(dirname "$0")"
if [ -f "$HOOK_DIR/_meta-mode.sh" ]; then
    source "$HOOK_DIR/_meta-mode.sh"
fi

# Read ff.config.json once, up front. The credential, ABOUTME and pipeline sections below
# each ask the config several questions; with the preload every answer is a variable.
if [ -f "$HOOK_DIR/_config-reader.sh" ]; then
    source "$HOOK_DIR/_config-reader.sh"
    ff_config_preload
fi

# Check meta-mode isolation (can be bypassed with CLAUDE_ALLOW_PRODUCTION_WRITE=true)
if [ "$CLAUDE_META_MODE" = "true" ] && [ "$CLAUDE_ALLOW_PRODUCTION_WRITE" != "true" ]; then
    # Get project root for path comparison
    PROJECT_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

    # Resolve symlinks in both paths to handle macOS /tmp → /private/tmp
    # For new files, resolve directory portion (file doesn't exist yet)
    _META_DIR="$(dirname "$FILE_PATH")"
    _META_RESOLVED_DIR="$(realpath "$_META_DIR" 2>/dev/null || echo "$_META_DIR")"
    RESOLVED_FILE_PATH="$_META_RESOLVED_DIR/$(basename "$FILE_PATH")"
    RESOLVED_PROJECT_ROOT="$(realpath "$PROJECT_ROOT" 2>/dev/null || echo "$PROJECT_ROOT")"

    # Compute relative path from both resolved and raw FILE_PATH.
    # When .meta/ is a symlink to an external directory (e.g., factory-workshop),
    # realpath resolves it outside PROJECT_ROOT, breaking the prefix strip.
    # Fall back to the raw FILE_PATH (relative to PROJECT_ROOT) for pattern matching.
    RELATIVE_PATH="${RESOLVED_FILE_PATH#$RESOLVED_PROJECT_ROOT/}"
    if [[ "$RELATIVE_PATH" == "$RESOLVED_FILE_PATH" ]]; then
        # Resolved path is outside project root — use raw path instead
        RELATIVE_PATH="${FILE_PATH#$RESOLVED_PROJECT_ROOT/}"
        # If still absolute, try stripping unresolved PROJECT_ROOT
        if [[ "$RELATIVE_PATH" == "$FILE_PATH" ]]; then
            RELATIVE_PATH="${FILE_PATH#$PROJECT_ROOT/}"
        fi
    fi

    # Only enforce meta-mode isolation for files INSIDE the project root.
    # Files outside (e.g., ~/.claude/plans/, ~/.claude/memory/) are not
    # production code — credential checks below still apply to them.
    if [[ "$RELATIVE_PATH" != "$FILE_PATH" ]]; then
        # Allowed paths in meta mode
        # - .meta/* - meta development files
        # - .claude/* - Claude Code configuration (hooks, plans, etc.)
        # - scripts/* - development scripts (often need updating)
        # - __tests__/* - test files (part of development)
        # - *.md in root - documentation files

        ALLOWED=false
        case "$RELATIVE_PATH" in
            .meta/*)
                ALLOWED=true
                ;;
            .claude/*)
                ALLOWED=true
                ;;
            scripts/*)
                ALLOWED=true
                ;;
            .github/*)
                ALLOWED=true
                ;;
            __tests__/*)
                ALLOWED=true
                ;;
            .env|.env.*)
                # .env files are gitignored local config, not production code
                ALLOWED=true
                ;;
            */CLAUDE.md)
                # Domain CLAUDE.md files are development documentation, not production code
                ALLOWED=true
                ;;
            *.md)
                # Root-level markdown files are docs
                if [[ "$RELATIVE_PATH" != */* ]]; then
                    ALLOWED=true
                fi
                ;;
        esac

        if [ "$ALLOWED" = "false" ]; then
            echo "BLOCKED: Meta mode active - changes to production code blocked!" >&2
            echo "" >&2
            echo "You are in META DEVELOPMENT mode (.meta/ directory exists)." >&2
            echo "Changes should go to .meta/ during meta-development." >&2
            echo "" >&2
            echo "Attempted to write: $RELATIVE_PATH" >&2
            echo "" >&2
            echo "Allowed paths in meta mode:" >&2
            echo "  - .meta/*" >&2
            echo "  - .claude/plans/*" >&2
            echo "  - .claude/archive/*" >&2
            echo "" >&2
            echo "To intentionally promote changes to production code:" >&2
            echo "  export CLAUDE_ALLOW_PRODUCTION_WRITE=true" >&2
            echo "" >&2
            echo "Or remove .meta/ directory to exit meta mode entirely." >&2
            echo "" >&2
            exit 2
        fi
    fi
fi

# ============================================
# LEARNINGS ARCHIVAL GUARD
# ============================================
# Blocks bulk truncation of learnings.md without a recent archive update.
# Prevents the doc-flywheel Step 3 archival step from being skipped.
# Bypass: SKIP_LEARNINGS_GUARD=true

if [[ "$SKIP_LEARNINGS_GUARD" != "true" ]] && \
   [[ "$FILE_PATH" =~ learnings\.md$ ]] && \
   [[ ! "$FILE_PATH" =~ learnings-archive\.md$ ]]; then
    # Resolve the actual file (may be behind a symlink)
    _LEARNINGS_DIR="$(dirname "$FILE_PATH")"
    _LEARNINGS_REAL_DIR="$(realpath "$_LEARNINGS_DIR" 2>/dev/null || echo "$_LEARNINGS_DIR")"
    _LEARNINGS_REAL="$_LEARNINGS_REAL_DIR/$(basename "$FILE_PATH")"
    if [ -f "$_LEARNINGS_REAL" ]; then
        _CURRENT_LINES=$(wc -l < "$_LEARNINGS_REAL" | tr -d ' ')
        _NEW_LINES=$(echo "$CONTENT" | wc -l | tr -d ' ')
        # Trigger: file >100 lines being reduced to <50% of current size
        if [ "$_CURRENT_LINES" -gt 100 ] && [ "$_NEW_LINES" -lt $(( _CURRENT_LINES / 2 )) ]; then
            _ARCHIVE_REAL="$_LEARNINGS_REAL_DIR/learnings-archive.md"
            _ARCHIVE_FRESH=false
            if [ -f "$_ARCHIVE_REAL" ]; then
                # GNU-first, matching _platform.sh's portable_stat_mtime: GNU stat
                # exits 1 on `-f %m` yet floods stdout, so BSD-first poisons the value.
                _ARCHIVE_MTIME=$(stat -c %Y "$_ARCHIVE_REAL" 2>/dev/null || stat -f %m "$_ARCHIVE_REAL" 2>/dev/null || echo 0)
                _NOW=$(date +%s)
                # Archive must have been modified within last 5 minutes
                if [ $(( _NOW - _ARCHIVE_MTIME )) -lt 300 ]; then
                    _ARCHIVE_FRESH=true
                fi
            fi
            if [ "$_ARCHIVE_FRESH" = "false" ]; then
                echo "BLOCKED: learnings.md is being reduced from $_CURRENT_LINES to $_NEW_LINES lines" >&2
                echo "without a recent update to learnings-archive.md." >&2
                echo "" >&2
                echo "Per doc-flywheel Step 3: ALWAYS copy entries to learnings-archive.md" >&2
                echo "before clearing them from learnings.md." >&2
                echo "" >&2
                echo "To fix: append entries to learnings-archive.md first, then clear." >&2
                echo "Override: SKIP_LEARNINGS_GUARD=true" >&2
                exit 2
            fi
        fi
    fi
fi

# ============================================
# RESOLVE PATHS FOR DOWNSTREAM CHECKS
# ============================================
# Resolve symlinks once for all downstream sections.
# macOS: /tmp → /private/tmp causes ${FILE_PATH#$PROJECT_ROOT/} to fail
# when git rev-parse returns /private/tmp but Claude passes /tmp.
# For new files (pre-write), realpath fails on the file itself because it
# doesn't exist yet. Resolve the directory portion instead, then reattach
# the filename. This handles the /tmp → /private/tmp symlink on macOS.
PROJECT_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
_FILE_DIR="$(dirname "$FILE_PATH")"
_RESOLVED_DIR="$(realpath "$_FILE_DIR" 2>/dev/null || echo "$_FILE_DIR")"
RESOLVED_FILE_PATH="$_RESOLVED_DIR/$(basename "$FILE_PATH")"
RESOLVED_PROJECT_ROOT="$(realpath "$PROJECT_ROOT" 2>/dev/null || echo "$PROJECT_ROOT")"

# ============================================
# CREDENTIAL SAFETY CHECK (CONFIG-DRIVEN)
# ============================================

# Skip credential checks for infrastructure/config files that legitimately
# contain credentials, plus test files, docs, and env examples.
# Uses a flag instead of exit 0 so downstream checks (assertion warnings,
# naming patterns) still run — those are specifically designed for .md files.
SKIP_CREDENTIALS=false

# Test files and docs
if [[ "$FILE_PATH" =~ \.test\.(js|ts|py|go|rs)$ ]] || [[ "$FILE_PATH" =~ \.spec\.(js|ts)$ ]] || \
   [[ "$FILE_PATH" =~ _test\.go$ ]] || \
   [[ "$FILE_PATH" =~ __tests__/ ]] || [[ "$FILE_PATH" =~ /tests?/ ]] || \
   [[ "$FILE_PATH" =~ \.md$ ]] || \
   [[ "$FILE_PATH" =~ \.env\.example$ ]] || [[ "$FILE_PATH" =~ \.env\.sample$ ]]; then
    SKIP_CREDENTIALS=true
fi

# Infrastructure/config files that legitimately contain credentials.
# These are gitignored or external to the repo — not application code.
if [[ "$(basename "$FILE_PATH")" =~ ^\.env(\..*)?$ ]] || \
   [[ "$FILE_PATH" =~ node_modules/ ]]; then
    SKIP_CREDENTIALS=true
fi

if [ "$SKIP_CREDENTIALS" = "false" ]; then
    # Check each configured credential pattern
    while IFS=$'\t' read -r PATTERN NAME EXCLUDE; do
        [ -z "$PATTERN" ] && continue

        if echo "$CONTENT" | grep -qE "$PATTERN"; then
            if [ -n "$EXCLUDE" ]; then
                # Check if match is excluded (e.g., env var reference)
                if echo "$CONTENT" | grep -E "$PATTERN" | grep -vqE "$EXCLUDE"; then
                    echo "BLOCKED: Hardcoded $NAME detected!" >&2
                    echo "" >&2
                    echo "Found pattern matching '$NAME' which appears to be a hardcoded credential." >&2
                    echo "Use environment variables instead." >&2
                    echo "" >&2
                    exit 2
                fi
            else
                echo "BLOCKED: Hardcoded $NAME detected!" >&2
                echo "" >&2
                echo "Found pattern matching '$NAME' which appears to be a hardcoded credential." >&2
                echo "Use environment variables instead." >&2
                echo "" >&2
                exit 2
            fi
        fi
    done < <(ff_credential_patterns)
fi

# ============================================
# MARKDOWN CREDENTIAL CHECK
# ============================================
# .md files are exempt from SID checks (credential patterns appear in format docs),
# but we still catch ACTUAL credential values — 32-char hex strings
# directly assigned to credential keywords. This catches real tokens
# leaked into test-results.md or similar documentation files.
# Pattern: keyword followed by separator then a quoted 32-char hex value
# e.g., auth_token: "ff5711..." or secret = "abc123..."

if [[ "$FILE_PATH" =~ \.md$ ]] && [ -n "$CONTENT" ]; then
    if echo "$CONTENT" | grep -qiE '(auth_token|_secret|password|authtoken)["'"'"'"]?[[:space:]]*[:=][[:space:]]*["'"'"'"][a-f0-9]{32}["'"'"'"]'; then
        echo "BLOCKED: Possible credential value in markdown file!" >&2
        echo "" >&2
        echo "Found a 32-character hex string adjacent to a credential keyword" >&2
        echo "in: $FILE_PATH" >&2
        echo "" >&2
        echo "If this is a real credential, replace it with [REDACTED] or a placeholder." >&2
        echo "If this is a format example, use a clearly fake value like:" >&2
        echo "  a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" >&2
        echo "" >&2
        exit 2
    fi
fi

# ============================================
# PROMPT INJECTION HEURISTIC CHECK
# ============================================

# Skip injection checks for documentation files (they legitimately discuss these topics)
SKIP_INJECTION=false
if [[ "$FILE_PATH" =~ \.md$ ]] || [[ "$FILE_PATH" =~ CLAUDE\.md$ ]] || \
   [[ "$FILE_PATH" =~ \.test\.(js|ts)$ ]] || [[ "$FILE_PATH" =~ __tests__/ ]] || \
   [[ "$FILE_PATH" =~ _safety-patterns\.sh$ ]]; then
    SKIP_INJECTION=true
fi

if [ "$SKIP_INJECTION" = "false" ] && [ -n "$CONTENT" ]; then
    source "$HOOK_DIR/_emit-event.sh"
    source "$HOOK_DIR/_safety-patterns.sh"

    if ! check_injection_patterns "$CONTENT" "file_content"; then
        echo "BLOCKED: Content contains text matching known prompt injection patterns." >&2
        echo "" >&2
        echo "This may be a false positive. If the content is legitimate:" >&2
        echo "  - Documentation files (.md) are exempt from this check" >&2
        echo "  - Test files are exempt from this check" >&2
        echo "  - Review the content and use Bash write if needed" >&2
        echo "" >&2
        exit 2
    fi
fi

# ============================================
# ABOUTME VALIDATION FOR NEW FILES (CONFIG-DRIVEN)
# ============================================

if [ -f "$HOOK_DIR/_config-reader.sh" ]; then
    # Get the relative path for header check
    # Uses RESOLVED_FILE_PATH/RESOLVED_PROJECT_ROOT from top-of-file resolution
    REL_PATH="${RESOLVED_FILE_PATH#$RESOLVED_PROJECT_ROOT/}"

    # Check if this file type requires headers
    if ff_requires_header "$REL_PATH"; then
        HEADER_PATTERN=$(ff_config ".fileHeaders.pattern" "ABOUTME:")

        # Check if file doesn't exist yet (new file) and content is being written (Write tool)
        if [ ! -f "$FILE_PATH" ] && [ ! -f "$RESOLVED_FILE_PATH" ] && [ "$HAS_CONTENT_KEY" = "1" ]; then
            # Validate header is present in content being written
            if ! echo "$CONTENT" | head -5 | grep -q "$HEADER_PATTERN"; then
                echo "BLOCKED: New file missing $HEADER_PATTERN comment!" >&2
                echo "" >&2
                echo "All source files must start with a 2-line $HEADER_PATTERN comment:" >&2
                echo "" >&2
                echo "// $HEADER_PATTERN [What this file does - action-oriented]" >&2
                echo "// $HEADER_PATTERN [Additional context - key behaviors, dependencies]" >&2
                echo "" >&2
                exit 2
            fi
        fi
    fi
fi

# ============================================
# PIPELINE GATE — New source files require tests (CONFIG-DRIVEN)
# ============================================

# Only check new files in tracked source directories (not tests, not helpers)
if [ -f "$HOOK_DIR/_config-reader.sh" ]; then
    # Uses RESOLVED_FILE_PATH/RESOLVED_PROJECT_ROOT from top-of-file resolution
    GATE_REL_PATH="${RESOLVED_FILE_PATH#$RESOLVED_PROJECT_ROOT/}"

    # Check if this is a new file in a tracked source directory
    IS_SOURCE_FILE=false
    TRACKED_DIRS=$(ff_config_array ".project.sourceDirectories" 2>/dev/null)
    if [ -n "$TRACKED_DIRS" ]; then
        # Normalize relative path: strip leading ./ for consistent matching
        GATE_NORM="${GATE_REL_PATH#./}"
        while IFS= read -r dir; do
            # Normalize dir: strip leading ./ and treat empty/. as root (match all)
            dir="${dir#./}"
            if [ -z "$dir" ] || [ "$dir" = "." ]; then
                IS_SOURCE_FILE=true
                break
            fi
            if [[ "$GATE_NORM" == "$dir"* ]]; then
                IS_SOURCE_FILE=true
                break
            fi
        done <<< "$TRACKED_DIRS"
    fi

    # Skip test files and non-source files (including Go _test.go convention)
    if [[ "$GATE_REL_PATH" =~ (\.test\.|\.spec\.|_test\.go$|__tests__|/tests?/) ]]; then
        IS_SOURCE_FILE=false
    fi

    if [ "$IS_SOURCE_FILE" = true ] && [ ! -f "$FILE_PATH" ] && [ ! -f "$RESOLVED_FILE_PATH" ]; then
        # This is a NEW source file — check for corresponding tests
        if [ "${SKIP_PIPELINE_GATE:-}" = "true" ]; then
            echo "Pipeline gate bypassed (SKIP_PIPELINE_GATE=true)" >&2
        else
            # Derive expected test path from the source file path
            # e.g., src/handlers/auth.js → test/handlers/auth.test.js
            GATE_BASENAME=$(basename "$GATE_REL_PATH")
            GATE_EXT="${GATE_BASENAME##*.}"
            GATE_NAME="${GATE_BASENAME%.*}"
            GATE_DIR=$(dirname "$GATE_REL_PATH")

            # Check for test file in common test locations
            TEST_FOUND=false
            for test_dir in __tests__ test tests; do
                for test_suffix in "test.${GATE_EXT}" "spec.${GATE_EXT}"; do
                    if [ -f "$PROJECT_ROOT/${test_dir}/${GATE_NAME}.${test_suffix}" ] || \
                       [ -f "$PROJECT_ROOT/${test_dir}/${GATE_DIR#*/}/${GATE_NAME}.${test_suffix}" ]; then
                        TEST_FOUND=true
                        break 2
                    fi
                done
                # pytest/unittest convention: tests/test_foo.py, not tests/foo.test.py.
                # Without this the gate blocks correct Python TDD and the only way past
                # is the override — the gate teaching people to ignore it.
                if [ -f "$PROJECT_ROOT/${test_dir}/test_${GATE_NAME}.${GATE_EXT}" ] || \
                   [ -f "$PROJECT_ROOT/${test_dir}/${GATE_DIR#*/}/test_${GATE_NAME}.${GATE_EXT}" ]; then
                    TEST_FOUND=true
                    break
                fi
            done
            # Also check for co-located tests (e.g., src/auth.test.js next to src/auth.js)
            if [ -f "$PROJECT_ROOT/${GATE_DIR}/${GATE_NAME}.test.${GATE_EXT}" ] || \
               [ -f "$PROJECT_ROOT/${GATE_DIR}/${GATE_NAME}.spec.${GATE_EXT}" ] || \
               [ -f "$RESOLVED_PROJECT_ROOT/${GATE_DIR}/${GATE_NAME}.test.${GATE_EXT}" ] || \
               [ -f "$RESOLVED_PROJECT_ROOT/${GATE_DIR}/${GATE_NAME}.spec.${GATE_EXT}" ]; then
                TEST_FOUND=true
            fi
            # Go convention: handler_test.go alongside handler.go
            if [ -f "$PROJECT_ROOT/${GATE_DIR}/${GATE_NAME}_test.${GATE_EXT}" ] || \
               [ -f "$RESOLVED_PROJECT_ROOT/${GATE_DIR}/${GATE_NAME}_test.${GATE_EXT}" ]; then
                TEST_FOUND=true
            fi
            # Rust convention: inline #[cfg(test)] module in the same source file
            if [ "$GATE_EXT" = "rs" ] && [ -n "$CONTENT" ] && echo "$CONTENT" | grep -q '#\[cfg(test)\]'; then
                TEST_FOUND=true
            fi

            if [ "$TEST_FOUND" = false ]; then
                echo "" >&2
                echo "BLOCKED: New source file has no corresponding tests!" >&2
                echo "" >&2
                echo "  Source:   ${GATE_REL_PATH}" >&2
                echo "  Expected: test file for ${GATE_NAME}.${GATE_EXT}" >&2
                echo "" >&2
                echo "This project enforces pipeline-driven development." >&2
                echo "For new features, use the development pipeline:" >&2
                echo "" >&2
                echo "  /architect [feature]   # Start with design review" >&2
                echo "  /test-gen [feature]    # Write failing tests first" >&2
                echo "  /dev [task]            # Then implement" >&2
                echo "" >&2
                echo "See .claude/references/workflow-patterns.md for full phase sequences." >&2
                echo "" >&2
                echo "Override: SKIP_PIPELINE_GATE=true (for emergency fixes)" >&2
                exit 2
            fi
        fi
    fi
fi

# ============================================
# HIGH-RISK ASSERTION WARNING (not blocking)
# ============================================

# Check documentation files for high-risk assertion patterns
if [[ "$FILE_PATH" =~ CLAUDE\.md$ ]] || [[ "$FILE_PATH" =~ \.claude/skills/.*\.md$ ]]; then
    WARNED=false

    # Check for negative behavioral claims without citation
    if echo "$CONTENT" | grep -qiE "(cannot|can't|not able to|impossible|not supported|not available)" && \
       ! echo "$CONTENT" | grep -qE "<!-- (verified|UNVERIFIED):"; then
        if [ "$WARNED" = false ]; then
            echo "" >&2
            echo "HIGH-RISK ASSERTION WARNING" >&2
            echo "   File: $FILE_PATH" >&2
            WARNED=true
        fi
        echo "   -> Negative behavioral claim detected (cannot/not supported/etc.)" >&2
    fi

    # Check for absolute claims about APIs/services without citation
    if echo "$CONTENT" | grep -qE "\b(always|never|must|only)\b" && \
       echo "$CONTENT" | grep -qiE "(api|webhook|endpoint|service)" && \
       ! echo "$CONTENT" | grep -qE "<!-- (verified|UNVERIFIED):"; then
        if [ "$WARNED" = false ]; then
            echo "" >&2
            echo "HIGH-RISK ASSERTION WARNING" >&2
            echo "   File: $FILE_PATH" >&2
            WARNED=true
        fi
        echo "   -> Absolute claim detected (always/never/must/only)" >&2
    fi

    # Check for numeric limits without citation (e.g., "max 16KB", "up to 4")
    if echo "$CONTENT" | grep -qE "(max|maximum|up to|at least|limit)[^a-z]*[0-9]+" && \
       ! echo "$CONTENT" | grep -qE "<!-- (verified|UNVERIFIED):"; then
        if [ "$WARNED" = false ]; then
            echo "" >&2
            echo "HIGH-RISK ASSERTION WARNING" >&2
            echo "   File: $FILE_PATH" >&2
            WARNED=true
        fi
        echo "   -> Numeric limit detected without citation" >&2
    fi

    if [ "$WARNED" = true ]; then
        echo "" >&2
        echo "   Did you verify these claims against official documentation?" >&2
        echo "   Add citations: <!-- verified: docs.example.com/... -->" >&2
        echo "   Or mark uncertain: <!-- UNVERIFIED: reason -->" >&2
        echo "" >&2
    fi
fi

# ============================================
# NON-EVERGREEN NAMING PATTERN WARNING (not blocking)
# ============================================

# Check for naming patterns that indicate temporal context
# These names will become misleading as codebase evolves
if echo "$CONTENT" | grep -qE "\b(Improved|Enhanced|Better|Refactored)[A-Z][a-zA-Z]*"; then
    MATCHED_NAMES=$(echo "$CONTENT" | grep -oE "\b(Improved|Enhanced|Better|Refactored)[A-Z][a-zA-Z]*" | head -5 | tr '\n' ', ' | sed 's/,$//')
    echo "" >&2
    echo "NON-EVERGREEN NAMING WARNING" >&2
    echo "   File: $FILE_PATH" >&2
    echo "   Found: $MATCHED_NAMES" >&2
    echo "" >&2
    echo "   Names like 'ImprovedX' or 'BetterY' become misleading over time." >&2
    echo "   What's 'improved' today will be 'old' tomorrow." >&2
    echo "   Use descriptive names that explain WHAT it does, not WHEN it was written." >&2
    echo "" >&2
fi

# Also check for "New" prefix followed by uppercase (but allow "new" in sentences)
if echo "$CONTENT" | grep -qE "(const|let|var|function|class|type|interface|def|func|fn)\s+New[A-Z]"; then
    MATCHED_NAMES=$(echo "$CONTENT" | grep -oE "(const|let|var|function|class|type|interface|def|func|fn)\s+New[A-Z][a-zA-Z]*" | sed 's/^[a-z]* //' | head -5 | tr '\n' ', ' | sed 's/,$//')
    echo "" >&2
    echo "NON-EVERGREEN NAMING WARNING" >&2
    echo "   File: $FILE_PATH" >&2
    echo "   Found: $MATCHED_NAMES" >&2
    echo "" >&2
    echo "   Names like 'NewHandler' will be outdated when you add another one." >&2
    echo "   Use descriptive names instead: 'StreamingHandler', 'BatchHandler', etc." >&2
    echo "" >&2
fi

exit 0
