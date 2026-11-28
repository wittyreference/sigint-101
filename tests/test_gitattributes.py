# ABOUTME: Tests the repository's .gitattributes in throwaway git repos: two branches that each add
# ABOUTME: an entry to an append-only ledger merge with no conflict and keep both entries.

import re
import shutil

import pytest

from tests.test_githooks import EMAIL, NAME, REPO, Sandbox

ATTRIBUTES = REPO / ".gitattributes"

# The files merged with git's built-in union driver: every entry stands alone and the order of
# entries from two branches does not matter. Each is (path, its text on the base branch).
LEDGERS = {
    ".claude/learnings.md": (REPO / ".claude" / "learnings.md").read_text(encoding="utf-8"),
}


def _entry(branch: str) -> str:
    # Every line names its branch, as real entries differ line by line. The union driver keeps
    # a line the two sides added identically at the same edge of their additions only once:
    # here the blank line closing each entry, so the merged entries end up with no blank line
    # between them. The text of each entry stays whole.
    return (f"## 2026-11-27: an entry written on {branch}\n"
            f"**Finding.** Only {branch} knows this.\n"
            f"**How to apply.** Read the {branch} entry after the merge.\n\n")


def _add_entry(text: str, entry: str, where: str) -> str:
    """The ledger with `entry` added where a writer puts it: below the header, before the first
    dated "## " entry (the ledger keeps the newest first), or at the end of the file."""
    if where == "end":
        return text + ("" if text.endswith("\n\n") else "\n") + entry
    first = text.find("\n## ")
    if first < 0:
        return text + ("" if text.endswith("\n\n") else "\n") + entry
    return text[:first + 1] + entry + text[first + 1:]


def _ledger_box(tmp_path, path: str) -> Sandbox:
    """A throwaway repo holding this repository's .gitattributes (when there is one) and the
    ledger at its own path, committed on main. No global or system git config reaches it."""
    box = Sandbox(tmp_path)
    box.env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    box.config("user.name", NAME)
    box.config("user.email", EMAIL)
    if ATTRIBUTES.exists():
        shutil.copyfile(ATTRIBUTES, box.path / ".gitattributes")
        assert box.git("add", ".gitattributes").returncode == 0
    ledger = box.path / path
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(LEDGERS[path], encoding="utf-8")
    assert box.git("add", path).returncode == 0
    assert box.git("commit", "-q", "-m", "base").returncode == 0
    return box


def _commit_entry_on(box: Sandbox, branch: str, path: str, where: str) -> None:
    assert box.git("checkout", "-q", "-b", branch, "main").returncode == 0
    ledger = box.path / path
    ledger.write_text(_add_entry(ledger.read_text(encoding="utf-8"), _entry(branch), where),
                      encoding="utf-8")
    assert box.git("commit", "-q", "-a", "-m", f"entry on {branch}").returncode == 0


@pytest.mark.parametrize("where", ["newest first", "end"])
@pytest.mark.parametrize("path", sorted(LEDGERS))
def test_two_branches_adding_entries_to_a_ledger_merge_cleanly_and_keep_both(
        tmp_path, path, where):
    box = _ledger_box(tmp_path, path)
    _commit_entry_on(box, "one", path, where)
    _commit_entry_on(box, "two", path, where)
    assert box.git("checkout", "-q", "one").returncode == 0

    merge = box.git("merge", "--no-ff", "-m", "merge two", "two")

    assert merge.returncode == 0, (
        f"merging two branches that each add an entry to {path} conflicted; .gitattributes "
        f"should give it 'merge=union':\n{merge.stdout}{merge.stderr}")
    merged = (box.path / path).read_text(encoding="utf-8")
    assert "<<<<<<<" not in merged and ">>>>>>>" not in merged
    # Read as tests/test_learnings_ledger.py reads the ledger: one entry per "## " heading.
    sections = [s.strip() for s in re.split(r"^(?=## )", merged, flags=re.MULTILINE)]
    for branch in ("one", "two"):
        assert _entry(branch).strip() in sections, (
            f"the entry written on {branch} is missing or no longer whole after the merge")
    assert box.git("status", "--porcelain").stdout == ""
