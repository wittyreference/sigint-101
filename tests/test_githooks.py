# ABOUTME: Tests the identity guard (githooks/pre-commit, pre-merge-commit) in throwaway git repos.
# ABOUTME: Commits and merges are made only when author and committer are the required identity.

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "githooks"
NAME = "wittyreference"
EMAIL = "mc8805@outlook.com"
REQUIRED = f"{NAME} <{EMAIL}> | {NAME} <{EMAIL}>"
# The wrong identity is invented: the repository is public, so no real person's name goes here.
OTHER_NAME = "Some Person"
OTHER_EMAIL = "x@example.com"


@dataclass
class Outcome:
    """A commit or merge outcome, booleans only, so a failing assert never prints an identity."""

    made: bool  # HEAD moved: the commit exists
    by_hook: bool  # the guard's own refusal is on stderr


class Sandbox:
    """A throwaway repository whose hooks are this repository's own githooks/ directory.

    Git reads no global or system config here (HOME and XDG_CONFIG_HOME are tmp_path and
    GIT_CONFIG_NOSYSTEM is set) and inherits no GIT_* or EMAIL variable, so the identity of
    whoever runs the suite can never stand in for the one a test sets.
    """

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "repo"
        self.path.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("GIT_") and k != "EMAIL"}
        self.env.update(HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path),
                        GIT_CONFIG_NOSYSTEM="1")
        self.files = 0
        assert self.git("init", "-q", "-b", "main").returncode == 0

    def git(self, *args, **env):
        return subprocess.run(["git", *args], cwd=self.path, env={**self.env, **env},
                              capture_output=True, text=True, timeout=20)

    def config(self, key, value):
        assert self.git("config", key, value).returncode == 0

    def arm(self):
        """Point git at the guard, as bootstrap.sh does."""
        self.config("core.hooksPath", str(HOOKS))

    def head(self):
        out = self.git("rev-parse", "-q", "--verify", "HEAD")
        return out.stdout.strip() if out.returncode == 0 else None

    def commit(self, *args, **env) -> Outcome:
        """Stage one new file and run `git commit` with these arguments and environment."""
        self.files += 1
        name = f"file{self.files}.txt"
        (self.path / name).write_text(f"{self.files}\n", encoding="utf-8")
        assert self.git("add", name).returncode == 0
        before = self.head()
        run = self.git("commit", "-q", "-m", f"change {self.files}", *args, **env)
        return Outcome(made=self.head() != before, by_hook="sigint101: refusing" in run.stderr)

    def merge(self, branch, **env) -> Outcome:
        """Merge `branch` with a merge commit git makes by itself, as git merge and git pull do."""
        before = self.head()
        run = self.git("merge", "-q", "--no-ff", "-m", f"merge {branch}", branch, **env)
        return Outcome(made=self.head() != before, by_hook="sigint101: refusing" in run.stderr)

    def identities(self):
        """The last commit's author and committer."""
        return self.git("log", "-1", "--format=%an <%ae> | %cn <%ce>").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Armed and set to the identity the repository requires; a test then breaks one part."""
    box = Sandbox(tmp_path)
    box.config("user.name", NAME)
    box.config("user.email", EMAIL)
    box.arm()
    return box


# --- the identity the repository requires ----------------------------------------------


def test_the_required_name_and_email_commit(repo):
    assert repo.commit().made
    assert repo.identities() == REQUIRED


def test_the_email_may_differ_in_case(repo):
    repo.config("user.email", EMAIL.upper())
    assert repo.commit().made


@pytest.mark.parametrize("hook", ["pre-commit", "pre-merge-commit"])
def test_the_guard_is_executable_bash(hook):
    # git skips a hook that is not executable, which would disarm the guard without a word.
    path = HOOKS / hook
    assert os.access(path, os.X_OK)
    run = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr


# --- the name: only the handle, exactly ------------------------------------------------


def test_a_wrong_name_with_the_right_email_is_refused(repo):
    # The hole this guard had: it read only the e-mail, so a global user.name (a person's own
    # name) went into a public history with the right address beside it.
    repo.config("user.name", OTHER_NAME)
    out = repo.commit()
    assert not out.made
    assert out.by_hook


def test_the_name_must_match_in_case_too(repo):
    repo.config("user.name", NAME.capitalize())
    out = repo.commit()
    assert not out.made
    assert out.by_hook


def test_an_author_name_in_config_over_user_name_is_refused(repo):
    # author.name wins over user.name for the author, so reading user.name alone misses it.
    repo.config("author.name", OTHER_NAME)
    out = repo.commit()
    assert not out.made
    assert out.by_hook


def test_a_wrong_author_name_in_the_environment_is_refused(repo):
    out = repo.commit(GIT_AUTHOR_NAME=OTHER_NAME)
    assert not out.made
    assert out.by_hook


# The committer is checked on its own: in these three only the committer name is wrong, so the
# author check, which runs first, cannot be what refuses them.


def test_a_wrong_committer_name_in_the_environment_is_refused(repo):
    out = repo.commit(GIT_COMMITTER_NAME=OTHER_NAME)
    assert not out.made
    assert out.by_hook


def test_a_committer_name_in_config_over_user_name_is_refused(repo):
    repo.config("committer.name", OTHER_NAME)
    out = repo.commit()
    assert not out.made
    assert out.by_hook


def test_a_handle_author_does_not_cover_a_wrong_committer_name(repo):
    # The leak's own shape: a person's name as user.name, and the handle given only as the author.
    repo.config("user.name", OTHER_NAME)
    out = repo.commit(f"--author={NAME} <{EMAIL}>")
    assert not out.made
    assert out.by_hook


@pytest.mark.parametrize("name", [NAME + "2", "x " + NAME])
def test_a_name_that_only_contains_the_handle_is_refused(repo, name):
    repo.config("user.name", name)
    out = repo.commit()
    assert not out.made
    assert out.by_hook


def test_an_inherited_nocasematch_does_not_loosen_the_name(repo):
    # An exported BASHOPTS turns shell options on as bash starts, and nocasematch makes [[ == ]]
    # ignore case. The author is checked first, so the case variant goes there.
    out = repo.commit(f"--author=WittyReference <{EMAIL}>", BASHOPTS="nocasematch")
    assert not out.made
    assert out.by_hook


def test_no_name_set_is_refused(tmp_path):
    box = Sandbox(tmp_path)
    box.config("user.email", EMAIL)
    box.arm()
    # With no name set, git falls back to the login's full name, or refuses an empty one itself
    # before any hook runs. Either way, no commit.
    assert not box.commit().made


# --- the e-mail ------------------------------------------------------------------------


def test_the_right_name_with_a_wrong_email_is_refused(repo):
    repo.config("user.email", OTHER_EMAIL)
    out = repo.commit()
    assert not out.made
    assert out.by_hook


def test_a_wrong_committer_email_in_the_environment_is_refused(repo):
    out = repo.commit(GIT_COMMITTER_EMAIL=OTHER_EMAIL)
    assert not out.made
    assert out.by_hook


def test_a_wrong_author_email_in_the_environment_is_refused(repo):
    # Only the author's address is wrong; the committer's is right, so this pins the author's.
    out = repo.commit(GIT_AUTHOR_EMAIL=OTHER_EMAIL)
    assert not out.made
    assert out.by_hook


@pytest.mark.parametrize("email", [EMAIL + ".example", "x" + EMAIL])
def test_an_email_that_only_contains_the_address_is_refused(repo, email):
    repo.config("user.email", email)
    out = repo.commit()
    assert not out.made
    assert out.by_hook


def test_no_email_set_is_refused(tmp_path):
    box = Sandbox(tmp_path)
    box.config("user.name", NAME)
    box.arm()
    # With no e-mail set, git falls back to login@host, or refuses a host it cannot name.
    # Either way, no commit.
    assert not box.commit().made


def test_an_identity_git_would_have_to_guess_is_refused_by_the_guard(tmp_path):
    # The author comes from the environment and is right; nothing names the committer. git
    # settles the committer only after pre-commit, so the guard meets the gap first and fails
    # closed rather than let git guess one from the login and host.
    box = Sandbox(tmp_path)
    box.arm()
    out = box.commit(GIT_AUTHOR_NAME=NAME, GIT_AUTHOR_EMAIL=EMAIL)
    assert not out.made
    assert out.by_hook


# --- another author on the command line ------------------------------------------------


def test_commit_author_option_is_shown_to_the_guard_and_refused(repo):
    # git commit exports the author it settled on (GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL) before
    # pre-commit runs, --author included, so the guard's `git var GIT_AUTHOR_IDENT` sees the
    # other name (observed with git 2.53).
    out = repo.commit(f"--author={OTHER_NAME} <{OTHER_EMAIL}>")
    assert not out.made
    assert out.by_hook


def test_amending_someone_elses_commit_is_refused_until_the_author_is_reset(tmp_path):
    box = Sandbox(tmp_path)
    box.config("user.name", NAME)
    box.config("user.email", EMAIL)
    # Another author's commit, already in history before the guard was armed.
    assert box.commit(f"--author={OTHER_NAME} <{OTHER_EMAIL}>").made
    box.arm()
    # --amend keeps that author, and git shows it to the guard.
    kept = box.commit("--amend")
    assert not kept.made
    assert kept.by_hook
    assert box.commit("--amend", "--reset-author").made
    assert box.identities() == REQUIRED


# --- merges: a merge git makes by itself runs pre-merge-commit, never pre-commit --------


def _diverge(box):
    """main and a side branch one commit apart each, so merging side needs a merge commit."""
    assert box.commit().made
    assert box.git("checkout", "-q", "-b", "side").returncode == 0
    assert box.commit().made
    assert box.git("checkout", "-q", "main").returncode == 0
    assert box.commit().made


def test_a_clean_merge_under_a_wrong_name_is_refused(repo):
    _diverge(repo)
    repo.config("user.name", OTHER_NAME)
    out = repo.merge("side")
    assert not out.made
    assert out.by_hook


def test_a_clean_merge_under_the_required_identity_is_made(repo):
    _diverge(repo)
    assert repo.merge("side").made
    assert repo.identities() == REQUIRED
