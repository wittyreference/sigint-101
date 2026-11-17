# ABOUTME: Tests the identity guard (githooks/pre-commit, pre-merge-commit, pre-push) in throwaway
# ABOUTME: git repos: commits, merges and pushes go only when author and committer are the handle.

import os
import shutil
import subprocess
from dataclasses import dataclass, field
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
    quiet: bool  # neither invented wrong value is anywhere in stdout or stderr
    abort_hint: bool  # stderr says how to back out of a refused merge


class Sandbox:
    """A throwaway repository whose hooks are this repository's own githooks/ directory.

    Git reads no global or system config here (HOME and XDG_CONFIG_HOME are tmp_path and
    GIT_CONFIG_NOSYSTEM is set) and inherits no GIT_* or EMAIL variable, so the identity of
    whoever runs the suite can never stand in for the one a test sets. Nor does it inherit the
    variables that change how bash starts (BASHOPTS, SHELLOPTS, BASH_ENV); a test sets them.
    """

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "repo"
        self.path.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("GIT_")
                    and k not in ("EMAIL", "BASHOPTS", "SHELLOPTS", "BASH_ENV")}
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
        """Point git at this repository's githooks/ by its absolute path. bootstrap.sh sets the
        relative path githooks instead, which names the same directory from a checkout's top."""
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
        return self.outcome(before, run)

    def merge(self, branch, **env) -> Outcome:
        """Merge `branch` with a merge commit git makes by itself, as git merge and git pull do."""
        before = self.head()
        run = self.git("merge", "-q", "--no-ff", "-m", f"merge {branch}", branch, **env)
        return self.outcome(before, run)

    def outcome(self, before, run) -> Outcome:
        said = run.stdout + run.stderr
        return Outcome(made=self.head() != before,
                       by_hook="sigint101: refusing" in run.stderr,
                       quiet=OTHER_NAME not in said and OTHER_EMAIL not in said,
                       abort_hint="git merge --abort" in run.stderr)

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


@pytest.mark.parametrize("hook", ["pre-commit", "pre-merge-commit", "pre-push"])
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


# --- what a refusal needs, and what it says --------------------------------------------


def _path_without(tmp_path, *missing):
    """A PATH holding every tool beside git and bash except `missing`.

    Built as tests/test_desktop_theme.py builds one without fontconfig: every tool in git's and
    bash's own directories is linked into one directory, leaving out the missing ones, and that
    directory is the whole PATH. Whatever the host has installed, the missing tools are gone.
    """
    tools = tmp_path / ("without-" + "-".join(missing))
    tools.mkdir()
    for directory in dict.fromkeys(Path(shutil.which(t)).parent for t in ("git", "bash")):
        for tool in directory.iterdir():
            link = tools / tool.name
            # The first directory to name a tool wins, as in a PATH lookup.
            if tool.name not in missing and not link.is_symlink():
                link.symlink_to(tool)
    for tool in missing:
        assert shutil.which(tool, path=str(tools)) is None, f"{tool} is still on the PATH"
    return str(tools)


@pytest.mark.parametrize("tool", ["tr", "cat"])
def test_a_missing_tool_never_lets_a_wrong_email_through(repo, tmp_path, tool):
    # A missing tool must leave the guard refusing, and still able to say why.
    repo.config("user.email", OTHER_EMAIL)
    out = repo.commit(PATH=_path_without(tmp_path, tool))
    assert not out.made
    assert out.by_hook


def test_without_tr_the_required_identity_still_commits(repo, tmp_path):
    assert repo.commit(PATH=_path_without(tmp_path, "tr")).made


def _shellopts(value):
    """An exported SHELLOPTS turns these options on in every bash that starts, the hook's too."""
    return {"SHELLOPTS": value} if value else {}


WRONG_VALUES = [("user.name", OTHER_NAME), ("user.email", OTHER_EMAIL)]


@pytest.mark.parametrize("shellopts", [None, "xtrace"])
@pytest.mark.parametrize("key, value", WRONG_VALUES)
def test_a_refused_commit_never_prints_the_wrong_value(repo, key, value, shellopts):
    # The wrong value may be a person's own name, and a refusal lands in terminals and transcripts.
    repo.config(key, value)
    out = repo.commit(**_shellopts(shellopts))
    assert not out.made
    assert out.by_hook
    assert out.quiet


@pytest.mark.parametrize("shellopts", [None, "xtrace"])
@pytest.mark.parametrize("key, value", WRONG_VALUES)
def test_a_refused_merge_never_prints_the_wrong_value(repo, key, value, shellopts):
    _diverge(repo)
    repo.config(key, value)
    out = repo.merge("side", **_shellopts(shellopts))
    assert not out.made
    assert out.by_hook
    assert out.quiet


def test_a_refused_merge_says_how_to_return_to_a_clean_tree(repo):
    _diverge(repo)
    before = repo.head()
    repo.config("user.name", OTHER_NAME)
    out = repo.merge("side")
    assert not out.made
    assert out.abort_hint
    # git leaves a refused merge staged, and the advice must be true.
    assert repo.git("merge", "--abort").returncode == 0
    assert repo.git("status", "--porcelain").stdout == ""
    assert repo.head() == before


# --- pushes: pre-push checks every commit that leaves, whatever made it ----------------
#
# A cherry-pick, rebase, am or revert that no conflict stops runs neither hook above and writes
# the configured name as committer, and git tag writes it as tagger with no hook at all.
# githooks/pre-push meets every commit and annotated tag a push sends instead.

COMMIT_FIELDS = ("author name", "author e-mail", "committer name", "committer e-mail")
FIELDS = COMMIT_FIELDS + ("tagger name", "tagger e-mail")
WRONG = (OTHER_NAME, OTHER_EMAIL)


@dataclass
class Pushed:
    """A push outcome. What git and the hook printed stays out of repr and comparisons, so a
    failing assert never prints an identity."""

    sent: bool  # the remote's refs changed
    by_hook: bool  # the guard's own refusal is on stderr
    quiet: bool  # neither invented wrong value is anywhere in stdout or stderr
    said: str = field(repr=False, compare=False)

    def says(self, *words):
        return all(word in self.said for word in words)

    def fields(self):
        """The identity fields the refusal names as wrong."""
        return [name for name in FIELDS if name in self.said]


class Clone(Sandbox):
    """A Sandbox whose remote, origin, is a throwaway bare repository beside it. Like every clone
    of this repository, it starts from a remote with history that it has fetched: here, one
    commit by the handle on main.

    make() and push_unchecked() go round the hooks, so a test can build the history it needs,
    such as a commit by another identity that the remote already holds, as it holds every squash
    merge GitHub commits. That is setup in throwaway repositories only: the rule against going
    round a hook is about real commits to this repository.
    """

    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        self.tmp = tmp_path
        self.remote = self.bare("remote.git")
        assert self.git("remote", "add", "origin", str(self.remote)).returncode == 0
        self.make()
        self.push_unchecked("main")

    def bare(self, name):
        """A new, empty bare repository beside the clone."""
        path = self.tmp / name
        assert self.git("init", "-q", "--bare", "-b", "main", str(path)).returncode == 0
        return path

    def make(self, author=(NAME, EMAIL), committer=(NAME, EMAIL)):
        """Commit one new file on the current branch as these identities, by plumbing, which
        runs no hook. Returns the commit's hash."""
        self.files += 1
        name = f"file{self.files}.txt"
        (self.path / name).write_text(f"{self.files}\n", encoding="utf-8")
        assert self.git("add", name).returncode == 0
        tree = self.git("write-tree").stdout.strip()
        parent = self.head()
        made = self.git("commit-tree", tree, *(["-p", parent] if parent else []),
                        "-m", f"change {self.files}",
                        GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1],
                        GIT_COMMITTER_NAME=committer[0], GIT_COMMITTER_EMAIL=committer[1])
        assert made.returncode == 0
        sha = made.stdout.strip()
        assert self.git("update-ref", "HEAD", sha).returncode == 0
        return sha

    def checkout(self, *args):
        assert self.git("checkout", "-q", *args).returncode == 0

    def short(self, sha):
        return self.git("rev-parse", "--short", sha).stdout.strip()

    def refs_at(self, bare):
        return self.git(f"--git-dir={bare}", "for-each-ref").stdout

    def push(self, *args, to="origin", **env) -> Pushed:
        """Run `git push <to>` with these arguments and environment, the hooks armed. `to` is a
        remote's name, or a bare repository's path given as a URL."""
        url = self.git("remote", "get-url", to)
        bare = url.stdout.strip() if url.returncode == 0 else to
        before = self.refs_at(bare)
        run = self.git("push", to, *args, **env)
        said = run.stdout + run.stderr
        return Pushed(sent=self.refs_at(bare) != before,
                      by_hook="sigint101: refusing to push" in run.stderr,
                      quiet=OTHER_NAME not in said and OTHER_EMAIL not in said,
                      said=said)

    def push_unchecked(self, *refs):
        """Put commits on the remote with the hooks off, as a merge made on GitHub lands there."""
        run = self.git("-c", "core.hooksPath=/dev/null", "push", "-q", "origin", *refs)
        assert run.returncode == 0


@pytest.fixture
def clone(tmp_path):
    """Armed and set to the identity the repository requires, with an empty remote."""
    box = Clone(tmp_path)
    box.config("user.name", NAME)
    box.config("user.email", EMAIL)
    box.arm()
    return box


def test_commits_under_the_required_identity_are_pushed(clone):
    clone.make()
    clone.make()
    update = clone.push("main")
    assert update.sent
    assert not update.by_hook
    clone.checkout("-b", "topic")
    clone.make()
    new_branch = clone.push("topic")
    assert new_branch.sent
    assert not new_branch.by_hook


def test_one_wrong_author_name_in_the_middle_refuses_the_push(clone):
    before = clone.make()
    wrong = clone.make(author=(OTHER_NAME, EMAIL))
    after = clone.make()
    out = clone.push("main")
    assert not out.sent
    assert out.by_hook
    # It names the ref, that commit and that field, and neither the right commits nor the value.
    assert out.says("refs/heads/main", clone.short(wrong))
    assert out.fields() == ["author name"]
    assert not out.says(clone.short(before)) and not out.says(clone.short(after))
    assert out.quiet
    # The advised base is where the branch meets the remote's main, so a branch that merged main
    # re-authors only its own commits, never copies of GitHub's squash merges.
    assert out.says("git rebase --exec", "--reset-author", "$(git merge-base HEAD origin/main)")


def test_a_wrong_committer_name_from_a_clean_rebase_is_refused(clone):
    # The other road: a rebase that no conflict stops runs no commit hook, so pre-commit never
    # meets the name it writes as committer.
    clone.make()
    assert clone.push("main").sent
    clone.checkout("-b", "topic")
    clone.make()
    assert clone.push("topic").sent
    clone.checkout("main")
    clone.make()
    assert clone.push("main").sent
    clone.checkout("topic")
    rebase = clone.git("-c", f"user.name={OTHER_NAME}", "rebase", "-q", "main")
    assert rebase.returncode == 0  # no hook stopped it
    out = clone.push("--force", "topic")
    assert not out.sent
    assert out.by_hook
    assert out.fields() == ["committer name"]
    assert out.quiet


def test_a_wrong_committer_email_is_refused(clone):
    clone.make()
    assert clone.push("main").sent
    wrong = clone.make(committer=(NAME, OTHER_EMAIL))
    out = clone.push("main")
    assert not out.sent
    assert out.by_hook
    assert out.says(clone.short(wrong))
    assert out.fields() == ["committer e-mail"]
    assert out.quiet


def test_a_new_branch_of_commits_the_remote_already_holds_is_pushed(clone):
    # The remote holds a commit by another identity, as it holds every squash merge GitHub
    # commits. A new branch made only of commits the remote holds sends none of them.
    clone.make()
    clone.make(committer=WRONG)
    clone.push_unchecked("main")
    assert clone.git("branch", "copy").returncode == 0
    out = clone.push("copy")
    assert out.sent
    assert not out.by_hook


def test_merging_in_commits_the_remote_already_holds_is_pushed(clone):
    # Bringing a branch up to date with main takes in what main holds, GitHub's squash merges
    # among them. Only the merge itself leaves, so only the merge is checked.
    clone.make()
    assert clone.push("main").sent
    clone.checkout("-b", "topic")
    clone.make()
    assert clone.push("topic").sent
    clone.checkout("main")
    clone.make(committer=WRONG)
    clone.push_unchecked("main")
    clone.checkout("topic")
    assert clone.merge("main").made
    out = clone.push("topic")
    assert out.sent
    assert not out.by_hook


def test_a_branch_delete_is_pushed(clone):
    # A delete sends no commit, and a branch that holds another identity must stay removable.
    clone.make()
    assert clone.push("main").sent
    clone.checkout("-b", "topic")
    clone.make(author=WRONG)
    clone.push_unchecked("topic")
    out = clone.push("--delete", "topic")
    assert out.sent
    assert not out.by_hook


def test_one_wrong_ref_among_several_refuses_the_whole_push(clone):
    clone.make()
    assert clone.push("main").sent
    clone.checkout("-b", "good")
    clone.make()
    clone.checkout("-b", "bad", "main")
    wrong = clone.make(author=(NAME, OTHER_EMAIL))
    out = clone.push("good", "bad")
    assert not out.sent  # the good branch did not go either
    assert out.by_hook
    assert out.says("refs/heads/bad", clone.short(wrong))
    assert not out.says("refs/heads/good")
    assert out.fields() == ["author e-mail"]
    assert out.quiet


def test_the_email_may_differ_in_case_in_a_push(clone):
    clone.make(author=(NAME, EMAIL.upper()), committer=(NAME, EMAIL.upper()))
    out = clone.push("main")
    assert out.sent
    assert not out.by_hook


@pytest.mark.parametrize("bashopts", [None, "nocasematch"])
def test_a_name_that_differs_only_in_case_is_refused_in_a_push(clone, bashopts):
    # An exported BASHOPTS=nocasematch would make the name comparison ignore case.
    clone.make(author=(NAME.capitalize(), EMAIL))
    out = clone.push("main", **({"BASHOPTS": bashopts} if bashopts else {}))
    assert not out.sent
    assert out.by_hook
    assert out.fields() == ["author name"]


@pytest.mark.parametrize("shellopts", [None, "xtrace"])
def test_a_refused_push_never_prints_the_wrong_value(clone, shellopts):
    clone.make(author=WRONG, committer=WRONG)
    out = clone.push("main", **_shellopts(shellopts))
    assert not out.sent
    assert out.by_hook
    assert out.fields() == list(COMMIT_FIELDS)
    assert out.quiet


def test_a_replace_ref_does_not_hide_a_commit_from_the_push_guard(clone):
    # git replace changes what git log and git cat-file show for a commit; a push still sends
    # the original commit, so the guard must read that one.
    clone.make()
    wrong = clone.make(author=WRONG)
    tree = clone.git("rev-parse", f"{wrong}^{{tree}}").stdout.strip()
    parent = clone.git("rev-parse", f"{wrong}^").stdout.strip()
    stand_in = clone.git("commit-tree", tree, "-p", parent, "-m", "stand-in")
    assert stand_in.returncode == 0
    assert clone.git("replace", wrong, stand_in.stdout.strip()).returncode == 0
    out = clone.push("main")
    assert not out.sent
    assert out.by_hook
    assert out.fields() == ["author name", "author e-mail"]


def test_a_remote_tip_this_clone_has_not_fetched_is_refused(clone):
    # The remote's branch moved on to a commit this clone lacks, so the guard cannot list what
    # the push would send. It refuses rather than guess, even when forced.
    clone.make()
    assert clone.push("main").sent
    at_remote = [f"--git-dir={clone.remote}"]
    tree = clone.git(*at_remote, "rev-parse", "main^{tree}").stdout.strip()
    moved = clone.git(*at_remote, "commit-tree", tree, "-p", "main", "-m", "elsewhere",
                      GIT_AUTHOR_NAME=NAME, GIT_AUTHOR_EMAIL=EMAIL,
                      GIT_COMMITTER_NAME=NAME, GIT_COMMITTER_EMAIL=EMAIL)
    assert moved.returncode == 0
    assert clone.git(*at_remote, "update-ref", "refs/heads/main",
                     moved.stdout.strip()).returncode == 0
    clone.make()
    out = clone.push("--force", "main")
    assert not out.sent
    assert out.by_hook


def _path_with_only(tmp_path, *wanted):
    """A PATH whose one directory holds links to these tools and nothing else. git puts its own
    exec-path first on the PATH it hands a hook, so its helpers are still found."""
    tools = tmp_path / ("only-" + "-".join(wanted))
    tools.mkdir()
    for tool in wanted:
        (tools / tool).symlink_to(shutil.which(tool))
    return str(tools)


def test_the_push_guard_needs_nothing_but_git_and_bash(clone, tmp_path):
    # No outside tool can go missing and open the check: with only git and bash on the PATH, a
    # right push still goes and a wrong one is still refused in the guard's own words.
    only = _path_with_only(tmp_path, "git", "bash")
    clone.make()
    assert clone.push("main", PATH=only).sent
    clone.make(author=WRONG)
    out = clone.push("main", PATH=only)
    assert not out.sent
    assert out.by_hook
    assert out.fields() == ["author name", "author e-mail"]


# --- tags: an annotated tag carries a tagger, and git tag runs no hook ------------------


def test_an_annotated_tag_with_a_wrong_tagger_name_is_refused(clone):
    # git tag -a writes the configured name as tagger, and no hook sees it before the push.
    tag = clone.git("-c", f"user.name={OTHER_NAME}", "tag", "-a", "-m", "release", "v1")
    assert tag.returncode == 0
    out = clone.push("v1")
    assert not out.sent
    assert out.by_hook
    assert out.says("refs/tags/v1")
    assert out.fields() == ["tagger name"]
    assert out.says("git tag -f -a")
    assert out.quiet


def test_tags_under_the_required_identity_are_pushed(clone):
    # An annotated tag whose tagger is the handle, and a lightweight tag, which has no tagger and
    # brings only commits the remote holds.
    assert clone.git("tag", "-a", "-m", "release", "v2").returncode == 0
    assert clone.git("tag", "v3").returncode == 0
    out = clone.push("v2", "v3")
    assert out.sent
    assert not out.by_hook


def test_a_wrong_tagger_on_a_tag_inside_a_tag_is_refused(clone):
    # A tag can point at another tag. The push sends both, and each carries a tagger.
    inner = clone.git("-c", f"user.name={OTHER_NAME}", "tag", "-a", "-m", "inner", "inner")
    assert inner.returncode == 0
    assert clone.git("tag", "-a", "-m", "outer", "outer", "inner").returncode == 0
    assert clone.git("tag", "-d", "inner").returncode == 0  # only the outer tag is pushed
    out = clone.push("outer")
    assert not out.sent
    assert out.by_hook
    assert out.says("refs/tags/outer")
    assert out.fields() == ["tagger name"]


def test_a_tag_whose_tagger_cannot_be_read_is_refused(clone):
    # A hand-made tag whose tagger line has no address in brackets still carries a name; the
    # guard cannot read it as an identity, so it fails closed on both fields.
    text = clone.tmp / "odd-tag.txt"
    text.write_text(f"object {clone.head()}\ntype commit\ntag odd\n"
                    f"tagger {OTHER_NAME} 1700000000 +0000\n\nodd\n", encoding="utf-8")
    made = clone.git("hash-object", "-t", "tag", "-w", "--literally", str(text))
    assert made.returncode == 0
    assert clone.git("update-ref", "refs/tags/odd", made.stdout.strip()).returncode == 0
    out = clone.push("odd")
    assert not out.sent
    assert out.by_hook
    assert out.fields() == ["tagger name", "tagger e-mail"]
    assert out.quiet


# --- where the guard cannot judge: no remote-tracking refs ------------------------------


@pytest.mark.parametrize("where", ["url", "unfetched remote", "mirror"])
def test_a_push_the_guard_cannot_judge_is_refused_in_one_line(clone, where):
    # With no remote-tracking refs for where the push goes, every commit would count as leaving,
    # the whole history with it, GitHub's squash merges included. The guard says so in a line
    # instead of listing that history; the same branch pushed to origin goes.
    clone.make(committer=WRONG)
    clone.push_unchecked("main")
    clone.checkout("-b", "topic")
    clone.make()
    if where == "url":
        out = clone.push("topic", to=str(clone.remote))
    elif where == "unfetched remote":
        assert clone.git("remote", "add", "backup", str(clone.bare("backup.git"))).returncode == 0
        out = clone.push("topic", to="backup")
    else:
        out = clone.push("--mirror", to=str(clone.bare("mirror.git")))
    assert not out.sent
    assert out.by_hook
    assert out.says("Push to origin, or fetch the remote first.")
    assert out.fields() == []
    assert not out.says("git rebase")
    assert out.quiet
    assert clone.push("topic").sent


def test_a_delete_by_url_goes(clone):
    # A delete sends nothing, so it needs no judging even where the guard cannot judge a push.
    clone.checkout("-b", "topic")
    clone.make()
    assert clone.push("topic").sent
    out = clone.push("--delete", "topic", to=str(clone.remote))
    assert out.sent
    assert not out.by_hook
