#!/bin/bash
# ABOUTME: Shared configuration reader for Feature Factory hooks.
# ABOUTME: Provides ff_config() and ff_config_array() to read ff.config.json via jq.

# Get project root (caller can override if already set)
export PROJECT_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

# Config file path
FF_CONFIG="$PROJECT_ROOT/ff.config.json"

# ff_config_preload — answer every question the per-edit hooks ask with ONE jq.
#
# Each helper below spawns a jq, and jq is about 100 ms of process start on a Pi 4. The
# pre-write hook used to ask the config seven questions per Edit, which is most of the
# stall a person feels before every file change. A hook that calls this once gets the
# same answers from shell variables; hooks that do not call it behave exactly as before.
#
# Only the keys the hot path reads are cached. Any other key falls through to jq, so a
# hook that asks for something new is never given a stale or missing answer.
#
# Booleans are emitted with an explicit null test, not `//`: jq's alternative operator
# treats false like null, so `.enabled // "true"` reports "true" for an explicit false.
ff_config_preload() {
    [ -n "${FF_PRELOADED:-}" ] && return 0
    { [ -f "$FF_CONFIG" ] && command -v jq &>/dev/null; } || return 1
    local rows
    rows=$(jq -r '
        def scalar(k; v): "\(k)\t\(if v == null then "" else (v | tostring) end)";
        def rows(k; v): (v // [] | .[]? | "\(k)\t\(.)");
        scalar("fileHeaders.enabled"; .fileHeaders.enabled),
        scalar("fileHeaders.sourceOnly"; .fileHeaders.sourceOnly),
        scalar("fileHeaders.pattern"; .fileHeaders.pattern),
        scalar("fileHeaders.requiredLines"; .fileHeaders.requiredLines),
        rows("fileHeaders.fileExtensions"; .fileHeaders.fileExtensions),
        rows("trackedDirectories"; .trackedDirectories),
        rows("project.sourceDirectories"; .project.sourceDirectories),
        (.credentialPatterns // [] | .[]? | "cred\t\(.pattern)\t\(.name)\t\(.excludePattern // "")")
    ' "$FF_CONFIG" 2>/dev/null) || return 1
    FF_C_FILEHEADERS_ENABLED=""; FF_C_FILEHEADERS_SOURCEONLY=""; FF_C_FILEHEADERS_PATTERN=""
    FF_C_FILEHEADERS_REQUIREDLINES=""; FF_C_FILEHEADERS_FILEEXTENSIONS=""
    FF_C_TRACKEDDIRECTORIES=""; FF_C_PROJECT_SOURCEDIRECTORIES=""; FF_C_CREDENTIALS=""
    local key rest
    while IFS=$'\t' read -r key rest; do
        case "$key" in
            fileHeaders.enabled)        FF_C_FILEHEADERS_ENABLED="$rest" ;;
            fileHeaders.sourceOnly)     FF_C_FILEHEADERS_SOURCEONLY="$rest" ;;
            fileHeaders.pattern)        FF_C_FILEHEADERS_PATTERN="$rest" ;;
            fileHeaders.requiredLines)  FF_C_FILEHEADERS_REQUIREDLINES="$rest" ;;
            fileHeaders.fileExtensions) FF_C_FILEHEADERS_FILEEXTENSIONS+="$rest"$'\n' ;;
            trackedDirectories)         FF_C_TRACKEDDIRECTORIES+="$rest"$'\n' ;;
            project.sourceDirectories)  FF_C_PROJECT_SOURCEDIRECTORIES+="$rest"$'\n' ;;
            cred)                       FF_C_CREDENTIALS+="$rest"$'\n' ;;
        esac
    done <<< "$rows"
    FF_PRELOADED=true
}

# Read a single value from ff.config.json
# Usage: ff_config ".testing.command" "npm test"
#   $1 = jq path (e.g., ".testing.command")
#   $2 = default value if key missing or file not found
ff_config() {
    local key="$1"
    local default="$2"
    local result
    if [ -n "${FF_PRELOADED:-}" ]; then
        # A cached key that came back empty was absent from the file the preload already
        # read, so the default is the answer; asking jq again would only cost the spawn.
        case "$key" in
            .fileHeaders.enabled)       echo "${FF_C_FILEHEADERS_ENABLED:-$default}"; return ;;
            .fileHeaders.sourceOnly)    echo "${FF_C_FILEHEADERS_SOURCEONLY:-$default}"; return ;;
            .fileHeaders.pattern)       echo "${FF_C_FILEHEADERS_PATTERN:-$default}"; return ;;
            .fileHeaders.requiredLines) echo "${FF_C_FILEHEADERS_REQUIREDLINES:-$default}"; return ;;
        esac
    fi
    if [ -f "$FF_CONFIG" ] && command -v jq &>/dev/null; then
        # An explicit null test rather than `// empty`: see ff_config_preload on why `//`
        # would turn a configured false into the default.
        result=$(jq -r "$key | if . == null then empty else . end" "$FF_CONFIG" 2>/dev/null)
        if [ -n "$result" ] && [ "$result" != "null" ]; then
            echo "$result"
        else
            echo "$default"
        fi
    else
        echo "$default"
    fi
}

# Read an array from ff.config.json, one element per line
# Usage: ff_config_array ".project.sourceDirectories"
ff_config_array() {
    local key="$1"
    if [ -n "${FF_PRELOADED:-}" ]; then
        case "$key" in
            .fileHeaders.fileExtensions) printf '%s' "$FF_C_FILEHEADERS_FILEEXTENSIONS"; return ;;
            .trackedDirectories)         printf '%s' "$FF_C_TRACKEDDIRECTORIES"; return ;;
            .project.sourceDirectories)  printf '%s' "$FF_C_PROJECT_SOURCEDIRECTORIES"; return ;;
        esac
    fi
    if [ -f "$FF_CONFIG" ] && command -v jq &>/dev/null; then
        jq -r "$key[]? // empty" "$FF_CONFIG" 2>/dev/null
    fi
}

# Read credential patterns as tab-separated: pattern\tname\texcludePattern
# Usage: while IFS=$'\t' read -r pattern name exclude; do ... done < <(ff_credential_patterns)
ff_credential_patterns() {
    if [ -n "${FF_PRELOADED:-}" ]; then
        printf '%s' "$FF_C_CREDENTIALS"
        return
    fi
    if [ -f "$FF_CONFIG" ] && command -v jq &>/dev/null; then
        jq -r '.credentialPatterns[]? | [.pattern, .name, (.excludePattern // "")] | join("\t")' "$FF_CONFIG" 2>/dev/null
    fi
}

# Read doc mappings as tab-separated: sourcePattern\tdocTarget
# Usage: while IFS=$'\t' read -r source doc; do ... done < <(ff_doc_mappings)
ff_doc_mappings() {
    if [ -f "$FF_CONFIG" ] && command -v jq &>/dev/null; then
        jq -r '.docMappings | to_entries[]? | [.key, .value] | join("\t")' "$FF_CONFIG" 2>/dev/null
    fi
}

# Check if a file path matches any tracked directory
# Usage: if ff_is_tracked_file "src/main.js"; then ...
ff_is_tracked_file() {
    local filepath="$1"
    # Normalize: strip leading ./ for consistent prefix matching
    filepath="${filepath#./}"
    local tracked_dirs
    tracked_dirs=$(ff_config_array ".trackedDirectories")
    if [ -z "$tracked_dirs" ]; then
        # No tracked directories configured, track everything
        return 0
    fi
    while IFS= read -r dir; do
        # Normalize: strip leading ./ and treat empty/. as root (match all)
        dir="${dir#./}"
        if [ -z "$dir" ] || [ "$dir" = "." ]; then
            return 0
        fi
        if [[ "$filepath" == "$dir"* ]]; then
            return 0
        fi
    done <<< "$tracked_dirs"
    return 1
}

# Check if a file should have ABOUTME headers based on config
# Usage: if ff_requires_header "src/main.js"; then ...
ff_requires_header() {
    local filepath="$1"
    local enabled
    enabled=$(ff_config ".fileHeaders.enabled" "true")
    if [ "$enabled" != "true" ]; then
        return 1
    fi

    local source_only
    source_only=$(ff_config ".fileHeaders.sourceOnly" "true")
    if [ "$source_only" = "true" ]; then
        if ! ff_is_tracked_file "$filepath"; then
            return 1
        fi
    fi

    # Check file extension
    local ext="${filepath##*.}"
    local extensions
    extensions=$(ff_config_array ".fileHeaders.fileExtensions")
    if [ -z "$extensions" ]; then
        return 1
    fi
    while IFS= read -r expected_ext; do
        # Strip leading dot for comparison
        expected_ext="${expected_ext#.}"
        if [ "$ext" = "$expected_ext" ]; then
            return 0
        fi
    done <<< "$extensions"
    return 1
}
