#!/bin/bash
# ABOUTME: Shared cross-platform helpers for macOS, Linux, and Windows (Git Bash/WSL).
# ABOUTME: Source this file from any hook or script that needs portable sed, stat, date, or notifications.

# Detect GNU vs BSD sed once at source time
if sed --version 2>/dev/null | grep -q GNU 2>/dev/null; then
    _PLATFORM_GNU_SED=true
else
    _PLATFORM_GNU_SED=false
fi

# portable_sed_i EXPRESSION FILE [FILE...]
#
# In-place sed that works on both BSD (macOS) and GNU (Linux/Git Bash).
# Usage is identical to: sed -i '' "EXPRESSION" FILE
#
# Example:
#   portable_sed_i "s/^FOO=.*/FOO=bar/" .env
portable_sed_i() {
    if [ "$_PLATFORM_GNU_SED" = true ]; then
        sed -i "$@"
    else
        local expr="$1"
        shift
        sed -i '' "$expr" "$@"
    fi
}

# portable_stat_mtime FILE
#
# Prints file modification time as epoch seconds.
# Returns "0" if the file doesn't exist or stat fails.
#
# GNU must be tried FIRST: GNU stat treats `-f %m` as "filesystem status of a
# file named %m" — it exits 1 (so the fallback still runs) but floods stdout
# with filesystem info, and command substitution captures BOTH commands'
# stdout, so the BSD-first order poisons the result with unparseable text.
# BSD stat rejects `-c` cleanly on stderr with nothing on stdout, so
# GNU-first is safe on every platform.
portable_stat_mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

# portable_date_relative DAYS_AGO [FORMAT]
#
# Prints a date N days in the past. Default format: %Y-%m-%d
# Works on both BSD date (macOS: -v flag) and GNU date (Linux: -d flag).
#
# Example:
#   portable_date_relative 7           # "2026-03-25"
#   portable_date_relative 30 %Y%m%d   # "20260302"
portable_date_relative() {
    local days="$1"
    local fmt="${2:-%Y-%m-%d}"
    date -u -v-"${days}d" +"$fmt" 2>/dev/null || date -u -d "${days} days ago" +"$fmt" 2>/dev/null
}

# portable_realpath PATH
#
# Resolves symlinks and returns absolute path.
# Falls back to python3 or plain echo if realpath is unavailable.
portable_realpath() {
    realpath "$1" 2>/dev/null \
        || python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$1" 2>/dev/null \
        || (cd "$(dirname "$1")" 2>/dev/null && echo "$(pwd)/$(basename "$1")") \
        || echo "$1"
}

# notify_desktop TITLE MESSAGE
#
# Cross-platform desktop notification with graceful fallback.
# macOS: osascript, Linux: notify-send, Windows/Git Bash: powershell, else: terminal bell.
notify_desktop() {
    local title="$1" message="$2"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        osascript -e "display notification \"$message\" with title \"$title\" sound name \"Glass\"" 2>/dev/null || true
    elif command -v notify-send &>/dev/null; then
        notify-send "$title" "$message" 2>/dev/null || true
    elif command -v powershell.exe &>/dev/null; then
        powershell.exe -Command "
            [void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')
            \$n = New-Object System.Windows.Forms.NotifyIcon
            \$n.Icon = [System.Drawing.SystemIcons]::Information
            \$n.Visible = \$true
            \$n.ShowBalloonTip(5000, '$title', '$message', 'Info')
        " 2>/dev/null || true
    else
        # Terminal bell as last resort
        printf '\a' 2>/dev/null || true
    fi
}

# portable_jq_install_hint
#
# Prints a platform-appropriate hint for installing jq.
portable_jq_install_hint() {
    if command -v brew &>/dev/null; then
        echo "Install: brew install jq" >&2
    elif command -v apt-get &>/dev/null; then
        echo "Install: sudo apt-get install -y jq" >&2
    elif command -v dnf &>/dev/null; then
        echo "Install: sudo dnf install -y jq" >&2
    elif command -v winget &>/dev/null; then
        echo "Install: winget install jqlang.jq" >&2
    elif command -v choco &>/dev/null; then
        echo "Install: choco install jq" >&2
    else
        echo "Install jq: https://jqlang.github.io/jq/download/" >&2
    fi
}
