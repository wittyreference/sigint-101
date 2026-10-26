#!/bin/bash
# ABOUTME: Pre-bash validation hook for git and deployment safety.
# ABOUTME: Blocks dangerous git operations and validates test status before deploy.

# Claude Code passes tool input as JSON on stdin, not env vars.
HOOK_INPUT=""
if [ ! -t 0 ]; then
    HOOK_INPUT="$(cat)"
fi

COMMAND=""
if [ -n "$HOOK_INPUT" ] && ! command -v jq &> /dev/null; then
    echo "BLOCKED: jq not installed — safety hooks cannot run. Install: brew install jq" >&2
    exit 2
fi
if [ -n "$HOOK_INPUT" ] && command -v jq &> /dev/null; then
    COMMAND="$(echo "$HOOK_INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)"
fi

# Exit if no command
if [ -z "$COMMAND" ]; then
    exit 0
fi

# Source config reader
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/_config-reader.sh" ]; then
    source "$SCRIPT_DIR/_config-reader.sh"
fi

# ============================================
# GIT COMMIT VALIDATION (No --no-verify)
# ============================================

if echo "$COMMAND" | grep -qE "git\s+commit.*--no-verify"; then
    echo "BLOCKED: git commit --no-verify is not allowed!" >&2
    echo "" >&2
    echo "The --no-verify flag bypasses pre-commit hooks which enforce code quality." >&2
    echo "If pre-commit hooks are failing, fix the underlying issues instead." >&2
    echo "" >&2
    exit 2
fi

# Also catch the short form -n
if echo "$COMMAND" | grep -qE "git\s+commit.*\s-n(\s|$)"; then
    echo "BLOCKED: git commit -n (--no-verify) is not allowed!" >&2
    echo "" >&2
    echo "Pre-commit hooks must run to ensure code quality." >&2
    echo "" >&2
    exit 2
fi

# ============================================
# SYMLINK LEAK PREVENTION (BLOCKING)
# ============================================
# Worktree setup symlinks node_modules and dist/ for efficiency.
# If those symlinks get staged, they poison every branch on checkout.
# Block any staged symlink (mode 120000) for a gitignored path.

if echo "$COMMAND" | grep -qE "^git\s+commit"; then
    # Check for ANY staged symlink (mode 120000) being added or present.
    # Exclude deletions (D) - removing a tracked symlink is the fix, not the problem.
    SYMLINKS=$(git diff --cached --diff-filter=d --raw 2>/dev/null | awk '/120000/{print $NF}')
    if [ -z "$SYMLINKS" ]; then
        # Also check the index directly for tracked symlinks in known-bad paths
        SYMLINKS=$(git ls-files --stage 2>/dev/null | awk '$1 == "120000" && ($4 == "node_modules" || $4 ~ /dist/) {print $4}')
    fi
    if [ -n "$SYMLINKS" ]; then
        BLOCKED=""
        for f in $SYMLINKS; do
            if git check-ignore -q "$f" 2>/dev/null; then
                BLOCKED="$BLOCKED $f"
            fi
        done
        if [ -n "$BLOCKED" ]; then
            echo "" >&2
            echo "BLOCKED: Staged symlinks for gitignored paths:$BLOCKED" >&2
            echo "" >&2
            echo "This usually means a worktree symlink leaked into the commit." >&2
            echo "Fix: git rm --cached$BLOCKED" >&2
            echo "" >&2
            exit 2
        fi
    fi
fi

# ============================================
# PRE-COMMIT DOCUMENTATION REMINDER
# ============================================

# Check if this is a git commit (but not the --no-verify checks above which already exited)
if echo "$COMMAND" | grep -qE "^git\s+commit"; then
    # Determine project root and source environment detection
    source "$SCRIPT_DIR/_meta-mode.sh"

    # ============================================
    # EPHEMERAL BRANCH WARNING
    # ============================================
    # Warn when committing to branches that look like validation/headless runs.
    # These branches should not accumulate feature work — commit to main instead.
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if echo "$CURRENT_BRANCH" | grep -qE "^(validation-|headless-|uber-val-|fresh-install-)"; then
        echo "" >&2
        echo "EPHEMERAL BRANCH: $CURRENT_BRANCH" >&2
        echo "" >&2
        echo "You are committing to what looks like a validation/test branch." >&2
        echo "If this is feature work, switch to main first:" >&2
        echo "" >&2
        echo "  git stash && git checkout main && git stash pop" >&2
        echo "" >&2
        echo "If this commit is intentionally on this branch, proceed." >&2
        echo "" >&2
    fi

    # ============================================
    # COVERAGE REGRESSION GUARDRAIL (BLOCKING)
    # ============================================
    # Prevents agents from deleting tests or reducing coverage.
    # Two checks: (1) test file deletion detection (instant, always runs),
    # (2) coverage baseline comparison (instant, runs if baseline exists).
    if [ "${SKIP_COVERAGE_CHECK:-}" != "true" ]; then
        # Check 1: Detect test file deletions in staged changes
        # pytest names tests tests/test_foo.py and foo_test.py, not foo.test.py, so the
        # stock JS-only pattern would let a pytest test deletion through unseen on a Python
        # project. Match both conventions alongside the original JS/TS/.test.py forms.
        DELETED_TESTS=$(git diff --staged --name-only --diff-filter=D 2>/dev/null \
            | grep -E '\.(test|spec)\.(js|ts|py)$|(^|/)test_[^/]*\.py$|_test\.py$' || true)
        if [ -n "$DELETED_TESTS" ]; then
            DELETED_COUNT=$(echo "$DELETED_TESTS" | wc -l | tr -d ' ')
            echo "" >&2
            echo "BLOCKED: $DELETED_COUNT test file(s) being deleted" >&2
            echo "" >&2
            echo "$DELETED_TESTS" | head -10 >&2
            echo "" >&2
            echo "Test files should not be deleted without explicit approval." >&2
            echo "If this is intentional, override: SKIP_COVERAGE_CHECK=true" >&2
            echo "" >&2
            exit 2
        fi

        # Check 2: Coverage baseline regression (if baseline exists)
        BASELINE_FILE="$PROJECT_ROOT/.coverage-baseline.json"
        CURRENT_COVERAGE="$PROJECT_ROOT/coverage/coverage-summary.json"
        if [ -f "$BASELINE_FILE" ] && [ -f "$CURRENT_COVERAGE" ] && command -v jq &>/dev/null; then
            # Compare each metric -- block if any drops more than 2%
            REGRESSION_FOUND=false
            REGRESSION_DETAILS=""
            for METRIC in statements branches functions lines; do
                BASELINE_VAL=$(jq -r ".coverage.$METRIC // 0" "$BASELINE_FILE" 2>/dev/null)
                CURRENT_VAL=$(jq -r ".total.$METRIC.pct // 0" "$CURRENT_COVERAGE" 2>/dev/null)
                DROP=$(echo "$BASELINE_VAL - $CURRENT_VAL" | bc -l 2>/dev/null || echo "0")
                if [ "$(echo "$DROP > 2" | bc -l 2>/dev/null || echo "0")" = "1" ]; then
                    REGRESSION_FOUND=true
                    REGRESSION_DETAILS="${REGRESSION_DETAILS}  $METRIC: ${CURRENT_VAL}% (was ${BASELINE_VAL}%, dropped ${DROP}%)\n"
                fi
            done
            if [ "$REGRESSION_FOUND" = true ]; then
                echo "" >&2
                echo "BLOCKED: Coverage regression detected (>2% drop)" >&2
                echo "" >&2
                printf "%b" "$REGRESSION_DETAILS" >&2
                echo "" >&2
                echo "Add tests to restore coverage before committing." >&2
                echo "To update baseline: ./scripts/save-coverage-baseline.sh" >&2
                echo "Override: SKIP_COVERAGE_CHECK=true git commit ..." >&2
                echo "" >&2
                exit 2
            fi
        fi
    fi

    # ============================================
    # META REFERENCE LEAKAGE WARNING
    # ============================================
    # Warn if staged files contain .meta/ references (potential leakage)
    if git diff --staged 2>/dev/null | grep -qE '\.meta/'; then
        echo "" >&2
        echo "WARNING: Staged changes reference .meta/" >&2
        echo "" >&2
        echo "This may indicate meta-development content leaking into shipped code." >&2
        echo "Review with: git diff --staged | grep '.meta/'" >&2
        echo "" >&2
        echo "If this is intentional documentation about the separation, proceed." >&2
        echo "" >&2
    fi

    # ============================================
    # LOCAL PATH LEAKAGE CHECK (BLOCKING)
    # ============================================
    # Block commits that ship hardcoded local directory paths.
    # These break for anyone who clones the repo to a different location.
    LOCAL_PATH_LEAKS=$(git diff --staged 2>/dev/null \
        | grep '^+' | grep -v '^+++' \
        | grep -En '/Users/[a-zA-Z0-9_.-]+/(workspaces|Desktop|Documents|Downloads|Library|Projects|repos|src|code|dev)/|/home/[a-zA-Z0-9_.-]+/(workspaces|Desktop|Documents|Downloads|Projects|repos|src|code|dev)/' \
        || true)
    if [ -n "$LOCAL_PATH_LEAKS" ]; then
        echo "" >&2
        echo "BLOCKED: Hardcoded local paths in staged changes" >&2
        echo "" >&2
        echo "These paths won't work for other users:" >&2
        echo "$LOCAL_PATH_LEAKS" | head -10 >&2
        echo "" >&2
        echo "Use dynamic alternatives:" >&2
        echo '  $HOME, $PROJECT_ROOT, $(git rev-parse --show-toplevel)' >&2
        echo '  $(pwd), relative paths, or $(dirname "$0")' >&2
        echo "" >&2
        echo "Override: SKIP_PATH_CHECK=true git commit ..." >&2
        echo "" >&2
        if [ "${SKIP_PATH_CHECK:-}" != "true" ]; then
            exit 2
        fi
    fi

    # ============================================
    # META-ONLY HOOK REGISTRATION CHECK (BLOCKING)
    # ============================================
    # Block commits that register meta-only scripts as shipped hooks.
    # A meta-only script exits immediately when CLAUDE_META_MODE != true,
    # meaning it does nothing for fresh-clone users — dead code in settings.json.
    if git diff --staged --name-only 2>/dev/null | grep -qF '.claude/settings.json'; then
        # Extract hook script filenames from added lines in the diff
        NEW_HOOK_FILES=$(git diff --staged -- .claude/settings.json 2>/dev/null \
            | grep '^+' | grep '\.sh"' \
            | grep -oE '[a-zA-Z0-9_-]+\.sh' \
            | sort -u)

        if [ -n "$NEW_HOOK_FILES" ]; then
            META_ONLY_HOOKS=""
            for hook_file in $NEW_HOOK_FILES; do
                # Resolve to full path (hooks live in .claude/hooks/)
                hook_path="$PROJECT_ROOT/.claude/hooks/$hook_file"
                [ -f "$hook_path" ] || continue

                # Check first 20 lines for meta-mode-only guard:
                #   if [ "$CLAUDE_META_MODE" != "true" ]; then exit 0; fi
                if head -20 "$hook_path" | tr '\n' ' ' \
                    | grep -qE 'CLAUDE_META_MODE.*!=.*true.*exit 0'; then
                    META_ONLY_HOOKS="${META_ONLY_HOOKS}  -> ${hook_file} (exits when not in meta-mode)\n"
                fi
            done

            if [ -n "$META_ONLY_HOOKS" ]; then
                echo "" >&2
                echo "BLOCKED: Meta-only script registered as a shipped hook" >&2
                echo "" >&2
                printf "%b" "$META_ONLY_HOOKS" >&2
                echo "" >&2
                echo "Scripts that exit 0 outside meta-mode do nothing for" >&2
                echo "fresh-clone users. Don't register them in settings.json." >&2
                echo "" >&2
                echo "Override: SKIP_META_HOOK_CHECK=true git commit ..." >&2
                echo "" >&2
                if [ "${SKIP_META_HOOK_CHECK:-}" != "true" ]; then
                    exit 2
                fi
            fi
        fi
    fi

    # ============================================
    # TYPE CHECK (BLOCKING) — config-driven
    # ============================================
    # Runs the language-appropriate type check for staged source files.
    # Supports: TypeScript (tsc), Go (go vet), Python (mypy), Rust (cargo check).
    # Override with .typeCheck.command in ff.config.json, or skip with SKIP_TSC_CHECK=true.
    TYPE_CHECK_CMD=$(ff_config ".typeCheck.command" "" 2>/dev/null)
    PROJECT_LANG=$(ff_config ".project.language" "javascript" 2>/dev/null)

    # Determine the type check command if not explicitly configured
    if [ -z "$TYPE_CHECK_CMD" ]; then
        case "$PROJECT_LANG" in
            javascript|typescript)
                # Only run tsc if there are staged .ts/.tsx files and a tsconfig.json
                STAGED_TS=$(git diff --staged --name-only 2>/dev/null | grep -E '\.(ts|tsx)$' || true)
                if [ -n "$STAGED_TS" ] && [ -f "$PROJECT_ROOT/tsconfig.json" ] && command -v npx &>/dev/null; then
                    TYPE_CHECK_CMD="npx tsc --noEmit"
                fi
                ;;
            go)
                STAGED_GO=$(git diff --staged --name-only 2>/dev/null | grep -E '\.go$' || true)
                if [ -n "$STAGED_GO" ] && command -v go &>/dev/null; then
                    TYPE_CHECK_CMD="go vet ./..."
                fi
                ;;
            python)
                STAGED_PY=$(git diff --staged --name-only 2>/dev/null | grep -E '\.py$' || true)
                if [ -n "$STAGED_PY" ] && command -v mypy &>/dev/null; then
                    TYPE_CHECK_CMD="mypy ."
                fi
                ;;
            rust)
                STAGED_RS=$(git diff --staged --name-only 2>/dev/null | grep -E '\.rs$' || true)
                if [ -n "$STAGED_RS" ] && command -v cargo &>/dev/null; then
                    TYPE_CHECK_CMD="cargo check"
                fi
                ;;
        esac
    fi

    if [ -n "$TYPE_CHECK_CMD" ]; then
        TYPE_CHECK_OUT=$(cd "$PROJECT_ROOT" && eval "$TYPE_CHECK_CMD" 2>&1 || true)
        TYPE_CHECK_EXIT=$?
        # Detect failures: tsc uses 'error TS', others use non-zero exit
        TYPE_CHECK_FAILED=false
        if echo "$TYPE_CHECK_OUT" | grep -q 'error TS'; then
            TYPE_CHECK_FAILED=true
        elif [ "$TYPE_CHECK_EXIT" -ne 0 ] && [ "$PROJECT_LANG" != "javascript" ] && [ "$PROJECT_LANG" != "typescript" ]; then
            TYPE_CHECK_FAILED=true
        fi

        if [ "$TYPE_CHECK_FAILED" = true ]; then
            echo "" >&2
            echo "BLOCKED: Type check failed ($TYPE_CHECK_CMD)" >&2
            echo "$TYPE_CHECK_OUT" | head -15 >&2
            echo "" >&2
            echo "Fix type errors before committing." >&2
            echo "Override: SKIP_TSC_CHECK=true git commit ..." >&2
            echo "" >&2
            if [ "${SKIP_TSC_CHECK:-}" != "true" ]; then
                exit 2
            fi
        fi
    fi

    # Call the consolidated flywheel-doc-check (environment-aware)
    FLYWHEEL_HOOK="$SCRIPT_DIR/flywheel-doc-check.sh"
    if [ -x "$FLYWHEEL_HOOK" ]; then
        "$FLYWHEEL_HOOK" --force
    fi

    # ============================================
    # AUTO-CLEAR ADDRESSED PENDING ACTIONS
    # ============================================
    PENDING_ACTIONS="$CLAUDE_PENDING_ACTIONS"
    if [ -f "$PENDING_ACTIONS" ]; then
        STAGED_FILES=$(git diff --staged --name-only 2>/dev/null)
        if [ -n "$STAGED_FILES" ]; then
            CLEARED_COUNT=0
            TEMP_FILE=$(mktemp)
            while IFS= read -r line; do
                if echo "$line" | grep -q "^\- \["; then
                    # Extract doc path: text between bullet and " - "
                    DOC_PATH=$(echo "$line" | sed -n 's/.*• \(.*\) - .*/\1/p')
                    # Resolve aliases
                    case "$DOC_PATH" in
                        "Root CLAUDE.md") RESOLVED="CLAUDE.md" ;;
                        "Verify doc-map.md"*) RESOLVED=".claude/references/doc-map.md" ;;
                        ".meta/"*) RESOLVED="" ;;  # gitignored, skip
                        "Relevant "*) RESOLVED="" ;;  # too vague, skip
                        *) RESOLVED="$DOC_PATH" ;;
                    esac
                    # Check if resolved path is in staged files
                    if [ -n "$RESOLVED" ] && echo "$STAGED_FILES" | grep -qF "$RESOLVED"; then
                        TIMESTAMP=$(echo "$line" | sed -n 's/.*\[\(.*\)\].*/\1/p')
                        echo "*Auto-cleared [$TIMESTAMP]: $DOC_PATH - staged in this commit*" >> "$TEMP_FILE"
                        CLEARED_COUNT=$((CLEARED_COUNT + 1))
                    else
                        echo "$line" >> "$TEMP_FILE"
                    fi
                else
                    echo "$line" >> "$TEMP_FILE"
                fi
            done < "$PENDING_ACTIONS"
            mv "$TEMP_FILE" "$PENDING_ACTIONS"
            if [ "$CLEARED_COUNT" -gt 0 ]; then
                echo "" >&2
                echo "Doc flywheel: Auto-cleared $CLEARED_COUNT pending action(s) addressed in this commit." >&2
            fi
        fi
    fi

    # ============================================
    # COMMIT CHECKLIST PROMPT (Non-blocking)
    # ============================================
    echo "" >&2
    echo "COMMIT CHECKLIST" >&2
    echo "  [ ] Updated todo if applicable?" >&2
    echo "  [ ] Captured learnings?" >&2
    echo "  [ ] Design decision documented if architectural?" >&2
    echo "" >&2

    # ============================================
    # PENDING DOCUMENTATION ACTIONS (BLOCKING)
    # ============================================
    if [ -f "$PENDING_ACTIONS" ]; then
        ACTION_COUNT=$(grep -c "^\- \[" "$PENDING_ACTIONS" 2>/dev/null || echo "0")
        if [ "$ACTION_COUNT" -gt 0 ]; then
            echo "" >&2
            echo "PENDING DOCUMENTATION ACTIONS ($ACTION_COUNT items)" >&2
            echo "" >&2
            grep "^\- \[" "$PENDING_ACTIONS" >&2
            echo "" >&2

            # Check for escape hatch (environment variable)
            if [ "$SKIP_PENDING_ACTIONS" = "true" ] || [ "$SKIP_PENDING_ACTIONS" = "1" ]; then
                echo "Skipping pending-actions check (SKIP_PENDING_ACTIONS set)" >&2
                echo "" >&2
            else
                echo "BLOCKED: Pending documentation actions must be addressed!" >&2
                echo "" >&2
                echo "Options:" >&2
                echo "  1. Address the pending actions and clear the file" >&2
                echo "  2. Override: SKIP_PENDING_ACTIONS=true git commit ..." >&2
                echo "" >&2
                echo "To clear after addressing:" >&2
                echo "  rm $PENDING_ACTIONS" >&2
                echo "" >&2
                exit 2
            fi
        fi
    fi
fi

# ============================================
# FORCE PUSH PROTECTION
# ============================================

if echo "$COMMAND" | grep -qE "git\s+push.*--force"; then
    if echo "$COMMAND" | grep -qE "\s(main|master)(\s|$)"; then
        echo "BLOCKED: Force push to main/master is not allowed!" >&2
        echo "" >&2
        echo "Force pushing to protected branches can cause data loss." >&2
        echo "If you need to revert changes, use 'git revert' instead." >&2
        echo "" >&2
        exit 2
    fi
fi

# ============================================
# DEPLOYMENT VALIDATION (CONFIG-DRIVEN)
# ============================================

DEPLOY_CMD=$(ff_config ".deployment.command" "" 2>/dev/null)

if [ -n "$DEPLOY_CMD" ] && echo "$COMMAND" | grep -qF "$DEPLOY_CMD"; then
    echo "Deployment detected - running pre-deployment validation..."

    # Check for uncommitted changes
    if [ -d ".git" ]; then
        UNCOMMITTED=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
        if [ "$UNCOMMITTED" -gt 0 ]; then
            echo "WARNING: You have $UNCOMMITTED uncommitted change(s)."
            echo "Consider committing before deployment."
            echo ""
        fi
    fi

    # Run tests (config-driven)
    TEST_CMD=$(ff_config ".testing.command" "npm test" 2>/dev/null)
    echo "Running tests..."
    if ! $TEST_CMD --silent 2>/dev/null; then
        echo "" >&2
        echo "BLOCKED: Tests are failing!" >&2
        echo "" >&2
        echo "All tests must pass before deployment." >&2
        echo "Run '$TEST_CMD' to see failures and fix them." >&2
        echo "" >&2
        exit 2
    fi
    echo "Tests passed"

    # Check code coverage (config-driven threshold)
    COVERAGE_CMD=$(ff_config ".testing.coverageCommand" "" 2>/dev/null)
    COVERAGE_MIN=$(ff_config ".testing.coverageThreshold" "80" 2>/dev/null)

    if [ -n "$COVERAGE_CMD" ]; then
        echo "Checking code coverage..."
        COVERAGE_SUMMARY="coverage/coverage-summary.json"

        if [ ! -f "$COVERAGE_SUMMARY" ] || [ "package.json" -nt "$COVERAGE_SUMMARY" ]; then
            $COVERAGE_CMD 2>/dev/null || true
        fi

        if [ -f "$COVERAGE_SUMMARY" ] && command -v jq &> /dev/null; then
            STATEMENTS=$(jq -r '.total.statements.pct // 0' "$COVERAGE_SUMMARY" 2>/dev/null)
            BRANCHES=$(jq -r '.total.branches.pct // 0' "$COVERAGE_SUMMARY" 2>/dev/null)

            COVERAGE_FAILED=false
            FAILED_METRICS=""

            if [ "$(echo "$STATEMENTS < $COVERAGE_MIN" | bc -l 2>/dev/null || echo "0")" = "1" ]; then
                COVERAGE_FAILED=true
                FAILED_METRICS="${FAILED_METRICS}statements: ${STATEMENTS}%, "
            fi
            if [ "$(echo "$BRANCHES < $COVERAGE_MIN" | bc -l 2>/dev/null || echo "0")" = "1" ]; then
                COVERAGE_FAILED=true
                FAILED_METRICS="${FAILED_METRICS}branches: ${BRANCHES}%, "
            fi

            if [ "$COVERAGE_FAILED" = true ]; then
                FAILED_METRICS=$(echo "$FAILED_METRICS" | sed 's/, $//')
                echo "" >&2
                echo "BLOCKED: Code coverage below ${COVERAGE_MIN}% threshold!" >&2
                echo "" >&2
                echo "Failed metrics: $FAILED_METRICS" >&2
                echo "" >&2
                exit 2
            fi
            echo "Coverage check passed"
        fi
    fi

    # Run linting (config-driven)
    LINT_CMD=$(ff_config ".linting.command" "" 2>/dev/null)
    if [ -n "$LINT_CMD" ]; then
        echo "Running linter..."
        if ! $LINT_CMD --silent 2>/dev/null; then
            echo "" >&2
            echo "BLOCKED: Linting errors detected!" >&2
            echo "" >&2
            echo "Fix linting errors before deployment." >&2
            echo "" >&2
            exit 2
        fi
        echo "Linting passed"
    fi

    echo "Pre-deployment validation complete."
    echo ""
fi

exit 0
