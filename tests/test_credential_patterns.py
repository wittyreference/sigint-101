# ABOUTME: Tests the pre-write hook's credential patterns as grep -E reads them, run as real bash.
# ABOUTME: Every payload is joined from parts at run time, so no line here has a credential's shape.

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / ".claude" / "hooks"
HOOK = HOOKS / "pre-write-validate.sh"
READER = HOOKS / "_config-reader.sh"
CONFIG = REPO / "ff.config.json"

# Every credential shape the config defines, by the name the hook prints when it refuses a write.
NAMES = [entry["name"] for entry in
         json.loads(CONFIG.read_text(encoding="utf-8"))["credentialPatterns"]]
PW_NAME = "Hardcoded password"

# The keywords the hook must catch and obviously fake values for them, joined at run time so that
# no scanner (this repo's own hook included) finds a hardcoded credential in this file.
PW = "pass" + "word"
KEY = "api" + "_key"
FAKE_PW = "x" * 14
FAKE_KEY = "x" * 28


def _assign(key: str, value: str) -> str:
    """One line assigning a quoted value to key: the shape the first two patterns look for."""
    return f'{key} = "{value}"'


# Read from the environment: there is no quoted value after the = for a pattern to match.
ENV_LOOKUP = PW + ' = os.environ["SIGINT101_X"]'
# A quoted fallback on the same line as an environment lookup: the pattern matches the line and
# the config's excludePattern, (os\.environ|getenv), spares it.
FALLBACK = _assign(PW, FAKE_PW) + ' if DEBUG else os.environ["SIGINT101_X"]'

# Per config entry: a line its pattern must catch, and a near miss it must let through.
SAMPLES = {
    PW_NAME: (_assign(PW, FAKE_PW), _assign(PW, "x" * 7)),
    "API key": (_assign(KEY, FAKE_KEY), _assign(KEY, "x" * 15)),
    "AWS Access Key ID": ("AKIA" + "0" * 16, "AKIA" + "0" * 15),
    "API key (sk-)": ("sk-" + "x" * 24, "sk-" + "x" * 19),
}

# Sources the config reader, takes one of its two read paths, and prints ff_credential_patterns
# either as it comes ("raw") or as the hook's own loop in pre-write-validate.sh reads it ("loop",
# one row per pattern, the fields split by a unit separator).
READ = r"""
source "$1"
if [ "$2" = preloaded ]; then
    ff_config_preload || exit 3
    [ -n "${FF_PRELOADED:-}" ] || exit 4
else
    [ -z "${FF_PRELOADED:-}" ] || exit 4
fi
if [ "$3" = raw ]; then
    ff_credential_patterns
    exit
fi
while IFS=$'\t' read -r PATTERN NAME EXCLUDE; do
    [ -z "$PATTERN" ] && continue
    printf '%s\x1f%s\x1f%s\n' "$PATTERN" "$NAME" "$EXCLUDE"
done < <(ff_credential_patterns)
"""


def _need(tool: str) -> None:
    # Fail, never skip: without it the hook checks nothing, and a skip would read as a pass.
    assert shutil.which(tool), f"{tool} is not on PATH; the hook needs it, so these tests fail"


def _env(project_root: Path, **extra: str) -> dict:
    """A clean environment, so nothing exported in the caller's shell can steer the hook."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PROJECT_ROOT": str(project_root),
            **extra}


def _write(tmp_path: Path, content: str) -> subprocess.CompletedProcess:
    """Run the real pre-write hook on a Write of an existing source file, as Claude Code would.

    The project root is a throwaway tree holding the repo's own ff.config.json and the file being
    written. The file already exists, so the new-file gates (ABOUTME, tests required) stay out of
    the way and only the credential check can refuse the write.
    """
    for tool in ("bash", "jq", "grep"):
        _need(tool)
    tree = tmp_path / "project"
    (tree / "sigint101").mkdir(parents=True)
    shutil.copy(CONFIG, tree / "ff.config.json")
    target = tree / "sigint101" / "lessons.py"
    shutil.copy(REPO / "sigint101" / "lessons.py", target)
    payload = {"session_id": "test-credential-patterns", "tool_name": "Write",
               "tool_input": {"file_path": str(target), "content": content}}
    return subprocess.run(["bash", str(HOOK)], input=json.dumps(payload), cwd=tree,
                          env=_env(tree, CLAUDE_LOGS_DIR=str(tmp_path / "logs")),
                          capture_output=True, text=True, timeout=30)


def _reader(path: str, form: str) -> str:
    """ff_credential_patterns on one read path: "preloaded" (the cached variable the hook uses)
    or "direct" (one jq per call), printed "raw" or as the hook's "loop" reads it."""
    for tool in ("bash", "jq"):
        _need(tool)
    result = subprocess.run(["bash", "-c", READ, "bash", str(READER), path, form],
                            env=_env(REPO), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"{path} read failed ({result.returncode}): {result.stderr}"
    assert result.stdout.strip(), f"the {path} path returned no credential patterns"
    return result.stdout


def _as_hook_reads(path: str) -> list:
    """(pattern, name, exclude) for each row, in order, as the hook's loop reads them."""
    return [tuple(row.split("\x1f")) for row in _reader(path, "loop").splitlines()]


def _grep_e(pattern: str, line: str) -> subprocess.CompletedProcess:
    """grep -E called the way the hook calls it: the pattern is the argument after -qE."""
    _need("grep")
    return subprocess.run(["grep", "-qE", pattern], input=line + "\n", capture_output=True,
                          text=True, timeout=10)


@pytest.mark.parametrize("lines, name", [
    ([_assign(PW, FAKE_PW)], PW_NAME),
    ([_assign(KEY, FAKE_KEY)], "API key"),
    # The exclude spares only the line it is on: a hardcoded value on the next line is refused.
    ([FALLBACK, _assign(PW, FAKE_PW)], PW_NAME),
], ids=["password", "api-key", "exclude-spares-only-its-own-line"])
def test_hook_refuses_a_hardcoded_credential(tmp_path, lines, name):
    result = _write(tmp_path, "\n".join(lines) + "\n")
    assert result.returncode == 2, f"allowed (exit {result.returncode}): {result.stderr}"
    assert "BLOCKED: Hardcoded" in result.stderr
    assert f"Found pattern matching '{name}'" in result.stderr


@pytest.mark.parametrize("line", [_assign("colour", FAKE_PW), ENV_LOOKUP, FALLBACK],
                         ids=["harmless-key", "environment-lookup", "excluded-fallback"])
def test_hook_allows_what_is_not_a_hardcoded_credential(tmp_path, line):
    result = _write(tmp_path, line + "\n")
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module")
def hook_patterns():
    return {name: (pattern, exclude) for pattern, name, exclude in _as_hook_reads("preloaded")}


@pytest.mark.parametrize("name", NAMES)
def test_grep_e_reads_every_configured_credential_shape(hook_patterns, name):
    # A new entry in ff.config.json fails here until it has samples that grep -E handles.
    assert name in SAMPLES, f"add a line {name!r} must catch and a near miss to SAMPLES"
    assert name in hook_patterns, f"the hook's loop never reads the pattern {name!r}"
    pattern, exclude = hook_patterns[name]
    hit, near_miss = SAMPLES[name]
    caught = _grep_e(pattern, hit)
    assert (caught.returncode, caught.stderr) == (0, ""), f"{pattern!r} on its sample"
    let_through = _grep_e(pattern, near_miss)
    assert (let_through.returncode, let_through.stderr) == (1, ""), f"{pattern!r} on a near miss"
    if exclude:
        # The hook runs the exclude through grep -E too; it must compile without a word.
        compiled = _grep_e(exclude, hit)
        assert compiled.returncode in (0, 1) and compiled.stderr == "", compiled.stderr


@pytest.mark.parametrize("path", ["preloaded", "direct"])
def test_config_reader_hands_ere_to_grep_on_both_paths(path):
    raw = _reader(path, "raw")
    # grep -E has no (?:...) group and no \s: the reader must have rewritten both.
    assert "(?:" not in raw
    assert "\\s" not in raw


def test_both_read_paths_give_the_hook_the_same_patterns():
    # The raw text differs only by the tab before an empty excludePattern, which read drops, so
    # the two paths are compared as the hook's loop reads them.
    assert _as_hook_reads("preloaded") == _as_hook_reads("direct")
