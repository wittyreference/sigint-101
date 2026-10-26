#!/bin/bash
# ABOUTME: Heuristic prompt injection detection for pre-write hooks.
# ABOUTME: Checks text against known injection/jailbreak patterns, emits safety events.

# Requires _emit-event.sh to be sourced first for emit_event().

# check_injection_patterns TEXT SOURCE
#
# Scans text for known prompt injection / jailbreak patterns.
# Returns 0 if clean, 1 if suspicious pattern detected.
#
# TEXT: the content to scan
# SOURCE: context label for logging ("file_content", "external_data")
#
# Emits a safety_check event on detection (requires _emit-event.sh sourced).
check_injection_patterns() {
    local text="$1"
    local source="${2:-unknown}"

    # Case-insensitive grep patterns for known injection/jailbreak attempts.
    # These are deliberately broad — false positives are warned, not blocked by default.
    # The caller decides whether to block (exit 2) or warn based on context.
    local patterns=(
        "ignore (all |your |any )?(previous|prior) instructions"
        "you are now (a |an )?"
        "act as (a |an )?different"
        "forget (all |your |any )?instructions"
        "disregard (all |your |any )?(previous|prior)"
        "new system prompt"
        "override (your |the )?system"
        "bypass (your |the )?safety"
        "pretend you (are|have|can)"
        "jailbreak"
        "DAN mode"
        "do anything now"
    )

    for pattern in "${patterns[@]}"; do
        if echo "$text" | grep -iqE "$pattern"; then
            # Emit safety event if emit_event is available
            if type emit_event &>/dev/null; then
                emit_event "safety_check" "$(jq -nc \
                    --arg check "injection_heuristic" \
                    --arg src "$source" \
                    --arg pattern "$pattern" \
                    --arg result "detected" \
                    '{check: $check, input_source: $src, matched_pattern: $pattern, result: $result}')"
            fi
            return 1
        fi
    done
    return 0
}
