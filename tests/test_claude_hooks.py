# ABOUTME: End-to-end tests for .claude/hooks (type-check gate, learnings Edit guard, worktree
# ABOUTME: flywheel) and doc accuracy in .claude/commands/review.md and .claude/skills/.

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / ".claude" / "hooks"
PRE_BASH = HOOKS / "pre-bash-validate.sh"
PRE_WRITE = HOOKS / "pre-write-validate.sh"
FLYWHEEL = HOOKS / "flywheel-doc-check.sh"
CONFIG = REPO / "ff.config.json"


def _clean_env(project_root: Path, **extra: str) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(project_root),
        "PROJECT_ROOT": str(project_root),
    }
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                         timeout=20)
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res


def _init_repo(path: Path, config_overrides: dict | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "wittyreference")
    _git(path, "config", "user.email", "mc8805@outlook.com")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if config_overrides:
        cfg.update(config_overrides)
    (path / "ff.config.json").write_text(json.dumps(cfg), encoding="utf-8")
    (path / ".claude").mkdir(exist_ok=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "ff.config.json", "README.md")
    _git(path, "commit", "-q", "-m", "seed")
    return path


def _run_hook(hook: Path, cwd: Path, payload: dict, *args: str, **extra_env: str):
    for tool in ("bash", "jq", "git"):
        assert shutil.which(tool), f"{tool} is required on PATH"
    return subprocess.run(
        ["bash", str(hook), *args],
        input=json.dumps(payload),
        cwd=cwd,
        env=_clean_env(cwd, **extra_env),
        capture_output=True,
        text=True,
        timeout=30,
    )


# --- C17: pre-bash-validate.sh type-check gate captures the real exit status ------------------


def test_pre_bash_type_check_blocks_on_nonzero_exit(tmp_path):
    # Use a real subprocess (like mypy/go vet), not shell builtin `exit 1` which exits the subshell
    # before `|| true` can mask it.
    cmd = "python3 -c 'import sys; print(\"sigint101/x.py:1: error: bad type\"); sys.exit(1)'"
    repo = _init_repo(tmp_path / "repo", {"typeCheck": {"command": cmd}})
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'test'"}}
    res = _run_hook(PRE_BASH, repo, payload)
    assert res.returncode == 2, f"expected exit 2, got {res.returncode}: {res.stderr}"
    assert "BLOCKED: Type check failed" in res.stderr
    assert "bad type" in res.stderr


def test_pre_bash_type_check_allows_passing_check_and_override(tmp_path):
    ok_repo = _init_repo(tmp_path / "ok", {"typeCheck": {"command": "true"}})
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'test'"}}
    ok = _run_hook(PRE_BASH, ok_repo, payload)
    assert ok.returncode == 0, ok.stderr

    bad_repo = _init_repo(tmp_path / "bad", {"typeCheck": {"command": "false"}})
    bypassed = _run_hook(PRE_BASH, bad_repo, payload, SKIP_TSC_CHECK="true")
    assert bypassed.returncode == 0, bypassed.stderr


# --- C18: pre-write-validate.sh learnings guard checks whole-file Write, not partial Edit -----


def _seed_learnings(tmp_path: Path) -> tuple[Path, Path]:
    tree = tmp_path / "project"
    (tree / ".claude").mkdir(parents=True)
    shutil.copy(CONFIG, tree / "ff.config.json")
    learnings = tree / ".claude" / "learnings.md"
    learnings.write_text("".join(f"line {i}\n" for i in range(150)), encoding="utf-8")
    return tree, learnings


def test_pre_write_allows_small_edit_to_large_learnings_file(tmp_path):
    tree, learnings = _seed_learnings(tmp_path)
    payload = {
        "session_id": "test-c18",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(learnings),
            "old_string": "line 1\n",
            "new_string": "line 1 updated\nline 1b\n",
        },
    }
    res = _run_hook(PRE_WRITE, tree, payload)
    assert res.returncode == 0, f"Edit falsely blocked ({res.returncode}): {res.stderr}"


def test_pre_write_still_blocks_whole_file_write_truncating_learnings(tmp_path):
    tree, learnings = _seed_learnings(tmp_path)
    payload = {
        "session_id": "test-c18",
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(learnings),
            "content": "# truncated\nonly two lines\n",
        },
    }
    res = _run_hook(PRE_WRITE, tree, payload)
    assert res.returncode == 2, f"Write truncation not blocked ({res.returncode}): {res.stderr}"
    assert "BLOCKED: learnings.md is being reduced" in res.stderr


# --- C19: flywheel-doc-check.sh runs in linked worktrees and drops dead todo.md block ---------


def _init_linked_worktree(tmp_path: Path) -> Path:
    main = _init_repo(tmp_path / "main")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "topic", str(wt))
    (wt / ".claude").mkdir(exist_ok=True)
    assert (wt / ".git").is_file(), "expected linked worktree with a .git file, not a directory"
    return wt


def test_flywheel_runs_in_a_linked_worktree_for_real_findings(tmp_path):
    wt = _init_linked_worktree(tmp_path)
    (wt / ".env").write_text("FOO=bar\n", encoding="utf-8")
    _git(wt, "add", "-f", ".env")
    res = _run_hook(FLYWHEEL, wt, {"session_id": "test-wt"}, "--force")
    assert res.returncode == 0, res.stderr
    pending = wt / ".claude" / "pending-actions.md"
    assert pending.is_file(), "flywheel skipped the linked worktree and wrote no pending actions"
    assert ".env changed but .env.example did not" in pending.read_text(encoding="utf-8")


@pytest.mark.parametrize("layout", ["main-repo", "linked-worktree"])
def test_flywheel_and_pre_bash_do_not_block_tracked_edits_on_missing_todo_md(tmp_path, layout):
    root = (
        _init_repo(tmp_path / "main")
        if layout == "main-repo"
        else _init_linked_worktree(tmp_path)
    )
    (root / "sigint101").mkdir(exist_ok=True)
    (root / "sigint101" / "sample.py").write_text(
        "# ABOUTME: Sample file.\n# ABOUTME: Used for worktree hook test.\n",
        encoding="utf-8",
    )
    _git(root, "add", "sigint101/sample.py")
    fw = _run_hook(FLYWHEEL, root, {"session_id": "test-tracked"}, "--force")
    assert fw.returncode == 0, fw.stderr
    pending = root / ".claude" / "pending-actions.md"
    assert not pending.exists(), f"unexpected pending actions: {pending.read_text()}"

    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'test'"}}
    pb = _run_hook(PRE_BASH, root, payload)
    assert pb.returncode == 0, f"commit blocked unexpectedly ({pb.returncode}): {pb.stderr}"


# --- D04: review command and skills match what this repository actually ships -----------------


@pytest.mark.parametrize("rel, dead_script", [
    (".claude/commands/review.md", "scripts/review-evidence.py"),
    (".claude/skills/autonomous-guide.md", "scripts/enable-autonomous.sh"),
    (".claude/skills/env-doctor.md", "scripts/env-doctor.sh"),
])
def test_claude_docs_mark_unshipped_scripts_honestly(rel, dead_script):
    text = (REPO / rel).read_text(encoding="utf-8")
    assert not (REPO / dead_script).exists()
    if dead_script in text:
        assert "not shipped in this repository" in text.lower(), (
            f"{rel} references missing {dead_script} without marking it not shipped")


def test_review_and_autonomous_docs_describe_actual_pre_commit_hook():
    review = (REPO / ".claude" / "commands" / "review.md").read_text(encoding="utf-8").lower()
    auto = (REPO / ".claude" / "skills" / "autonomous-guide.md").read_text(encoding="utf-8").lower()
    assert "refuses staged code that lacks review evidence" not in review
    assert "pre-commit identity/comment/review-evidence" not in auto
    assert "identity" in review
    assert "identity" in auto
