#!/usr/bin/env bash
# ABOUTME: PreToolUse gate — writes to source directories must happen in a linked worktree.
# ABOUTME: Scoped to elmer/, scripts/ and githooks/ so docs and review sessions never meet it.

# The worktree invariant in .claude/rules/worktree-invariants.md has been documentation with
# nothing behind it, which is the shape the TX gate was in until it grew a caller. This is
# the caller. It is deliberately narrow: feature work on source is what benefits from
# isolation, while notes, analysis and the hardware corpus are exactly the writing a
# read-only session does, and a gate that fires during those trains you to bypass it.

set -uo pipefail

INPUT="$(cat 2>/dev/null || true)"
[ -n "$INPUT" ] || exit 0

# A missing jq is already fatal upstream: pre-write-validate.sh refuses every write without
# it. Duplicating that refusal here would only make the deadlock harder to read, so this
# gate stands down and lets the tool that owns the message own it.
command -v jq >/dev/null 2>&1 || exit 0

FILE_PATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$FILE_PATH" ] || exit 0

# Directories this gate guards, relative to the repo root. Overridable in ff.config.json
# under worktree.guardedPaths; the defaults are the three directories that hold executable
# source. Tests live outside it on purpose — a test-only change is the smallest safe edit
# there is, and blocking it would push TDD onto the main tree anyway.
GUARDED_DEFAULT=$'elmer/\nscripts/\ngithooks/'
ROOT_FOR_CONF="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
CONF="$ROOT_FOR_CONF/ff.config.json"

# Both config answers come back from ONE jq: the on/off switch on the first line, the guarded
# paths on the rest. jq costs about 100 ms of process startup on a Pi 4, and this hook runs
# before every single Write and Edit, so each avoided spawn is felt by the person typing.
#
# Absent config means on, so a fresh clone is guarded before anyone configures anything;
# only an explicit false turns it off. A missing file must not read as consent.
#
# Not `.worktree.enabled // true`: jq's alternative operator treats `false` the same as
# absent, so that expression returns true for `"enabled": false` and the off switch would
# never fire. Any jq default over a boolean has this bug.
CONF_READ="$(jq -r --arg d "$GUARDED_DEFAULT" '
    (if .worktree.enabled == false then "false" else "true" end),
    (if .worktree.guardedPaths then .worktree.guardedPaths[] else $d end)
' "$CONF" 2>/dev/null)"

if [ -z "$CONF_READ" ]; then
    # jq could not read the file at all. Guard by the defaults rather than stand down.
    ENABLED=true
    GUARDED="$GUARDED_DEFAULT"
else
    ENABLED="${CONF_READ%%$'\n'*}"
    GUARDED="${CONF_READ#*$'\n'}"
    # A single line back means the switch answered and no paths followed.
    [ "$GUARDED" = "$ENABLED" ] && GUARDED=""
fi
[ "$ENABLED" = "false" ] && exit 0
[ -n "$GUARDED" ] || GUARDED="$GUARDED_DEFAULT"

# Ask git about where the FILE lives, not where the session happens to be standing — a
# session can sit in one tree and write into another, and the file's own tree is the one
# whose isolation is at stake. A new file has no directory yet, so walk up to the deepest
# ancestor that exists.
PROBE="$(dirname "$FILE_PATH")"
while [ ! -d "$PROBE" ] && [ "$PROBE" != "/" ] && [ "$PROBE" != "." ]; do
    PROBE="$(dirname "$PROBE")"
done

if ! command -v git >/dev/null 2>&1; then
    # Fail OPEN, and say so. This guards a workflow convention, not a disk: a misplaced
    # commit is recoverable, while blocking here would leave the session unable to write to
    # source at all, and SKIP_WORKTREE_GATE cannot be injected into a session already
    # running. That is the deadlock .claude/rules/bypass-governance.md describes, and its
    # only exit is the `!` escape. Loud beats mute — a gate that stands down in silence is
    # the fail-open shape this repo spent a session removing.
    echo "worktree-gate: could not run git, so the worktree check was skipped for $FILE_PATH" >&2
    exit 0
fi

TOPLEVEL="$(git -C "$PROBE" rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "$TOPLEVEL" ] || exit 0

# Scratchpad and system paths sit outside any repo, and the gate has no opinion on them.
case "$FILE_PATH" in
    "$TOPLEVEL"/*) RELATIVE="${FILE_PATH#"$TOPLEVEL"/}" ;;
    *) exit 0 ;;
esac

GUARDED_HIT=""
while IFS= read -r prefix; do
    [ -n "$prefix" ] || continue
    case "$RELATIVE" in
        "$prefix"*) GUARDED_HIT="$prefix"; break ;;
    esac
done <<< "$GUARDED"
[ -n "$GUARDED_HIT" ] || exit 0

# The whole test. In the main worktree these two resolve to the same directory; in a linked
# worktree --git-dir points at .git/worktrees/<name> while --git-common-dir still points at
# the shared .git, so a difference IS the isolation this gate exists to require.
#
# Both raw values are checked BEFORE either is resolved, because `cd ""` succeeds without
# moving: an empty common-dir would resolve to the working directory, differ from the git
# dir, and hand back "linked worktree, allow" — the gate's pass outcome manufactured by a
# malfunction rather than by isolation.
GIT_DIR_RAW="$(git -C "$PROBE" rev-parse --absolute-git-dir 2>/dev/null)"
COMMON_RAW="$(git -C "$PROBE" rev-parse --git-common-dir 2>/dev/null)"
if [ -z "$GIT_DIR_RAW" ] || [ -z "$COMMON_RAW" ]; then
    # Refuse rather than stand down. git is present and answering — an unusable answer to
    # this one query is an anomaly, not a broken environment, so the operator still has
    # every normal tool and the block is recoverable. Standing down is reserved for the
    # case above, where the hook cannot run at all.
    echo "worktree-gate: git cannot describe $PROBE — refusing rather than guessing." >&2
    echo "  --absolute-git-dir='$GIT_DIR_RAW' --git-common-dir='$COMMON_RAW'" >&2
    exit 2
fi

# Resolve both to real paths before comparing: --git-common-dir can come back relative.
GIT_DIR="$(cd "$GIT_DIR_RAW" 2>/dev/null && pwd -P)"
COMMON_DIR="$(cd "$PROBE" && cd "$COMMON_RAW" 2>/dev/null && pwd -P)"
[ "$GIT_DIR" = "$COMMON_DIR" ] || exit 0   # linked worktree — this is the sanctioned state

if [ "${SKIP_WORKTREE_GATE:-}" = "true" ]; then
    # Tier-1 bypasses are never silent, per .claude/rules/bypass-governance.md — the record
    # of the override is the only thing that makes its use reviewable afterwards.
    echo "worktree-gate: BYPASSED via SKIP_WORKTREE_GATE for $RELATIVE (Tier 1 — needs MC's word)" >&2
    # In a subshell, and swallowing its status: the emitter sources two further helpers,
    # any of which can abort under `set -u` or call exit outright. Telemetry must never get
    # a vote on whether a tool call proceeds — the stderr line above is the record that
    # matters, and this is the copy for later review.
    HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$HOOK_DIR/_emit-event.sh" ]; then
        (
            # shellcheck disable=SC1091
            source "$HOOK_DIR/_emit-event.sh"
            emit_event "bypass_used" "$(jq -nc --arg p "$RELATIVE" \
                '{mechanism:"SKIP_WORKTREE_GATE",tier:1,path:$p,gate:"worktree"}')"
        ) >/dev/null 2>&1 || true
    fi
    exit 0
fi

cat >&2 <<EOF
BLOCKED: $RELATIVE is under $GUARDED_HIT and this is the main worktree.

Source changes belong on an isolated branch — see .claude/rules/worktree-invariants.md.
Start one, then make the edit there:

    EnterWorktree, or:  git worktree add -b <purpose-name> ../elmer-<purpose-name>

Name it for the purpose (fix-sweep-death), not temp. Docs, analysis/, todo.md, tests/ and
the hardware corpus are NOT guarded — if this write is one of those, the path is wrong.

SKIP_WORKTREE_GATE=true overrides this. It is Tier 1: ask MC first, never self-authorize.
EOF
exit 2
