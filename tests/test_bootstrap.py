# ABOUTME: Tests bootstrap.sh in a throwaway copy whose PATH holds stubs only, so nothing installs:
# ABOUTME: a missing node or jq gets a warning naming what fails, and setup still runs to the end.

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
DIRNAME = shutil.which("dirname")


def _stub(path: Path, body: str):
    """An executable bash script, with bash's absolute path as its shebang: PATH has no env."""
    path.write_text(f"#!{BASH}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


class Box:
    """A copy of bootstrap.sh and requirements.txt, run with a PATH of stubs and nothing else.

    python3, git and the venv's pip, ruff and pytest are stubs that record their arguments, so no
    venv is made, nothing is downloaded and no real git config changes. The venv's bin/ is made
    up front, as a venv that already exists; bootstrap.sh re-runs over one anyway. node and jq are
    on the PATH only when a test puts them there.
    """

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "repo"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "calls.log"
        self.root.mkdir()
        self.bin.mkdir()
        shutil.copy2(REPO / "bootstrap.sh", self.root / "bootstrap.sh")
        shutil.copy2(REPO / "requirements.txt", self.root / "requirements.txt")
        venv_bin = self.root / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        for name in ("pip", "ruff", "pytest"):
            _stub(venv_bin / name, f'echo "{name} $*" >> "{self.log}"\necho "{name} stub 0.0"')
        for name in ("python3", "git"):
            _stub(self.bin / name, f'echo "{name} $*" >> "{self.log}"')
        (self.bin / "dirname").symlink_to(DIRNAME)
        self.env = {"PATH": str(self.bin), "HOME": str(tmp_path), "LANG": "C.UTF-8"}

    def add_node(self):
        _stub(self.bin / "node", f'echo "node $*" >> "{self.log}"\necho v22.0.0')

    def add_jq(self):
        _stub(self.bin / "jq", f'echo "jq $*" >> "{self.log}"\necho jq-1.7')

    def run(self):
        result = subprocess.run([BASH, str(self.root / "bootstrap.sh")], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=30)
        calls = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        return result, calls


@pytest.fixture
def box(tmp_path):
    assert BASH and DIRNAME, "bash and dirname are needed to run bootstrap.sh"
    return Box(tmp_path)


def _setup_ran(result, calls):
    """bootstrap.sh finished: exit 0, requirements installed, hooks armed, versions reported."""
    assert result.returncode == 0, result.stdout + result.stderr
    assert "-r requirements.txt" in calls, calls
    assert "git config core.hooksPath githooks" in calls, calls
    assert "ruff --version" in calls and "pytest --version" in calls, calls


def test_a_missing_node_gets_a_warning_that_names_make_selftest(box):
    # make selftest runs the panels' tests under node (tests/test_js_panels.py), and it fails
    # without one. bootstrap.sh is the setup step, so it is where a missing node is said.
    result, calls = box.run()
    _setup_ran(result, calls)
    warned = [line for line in result.stderr.lower().splitlines()
              if "node" in line and "make selftest" in line]
    assert warned, "no warning on stderr naming node and make selftest:\n" + result.stderr


def test_with_node_on_the_path_there_is_no_node_warning(box):
    box.add_node()
    result, calls = box.run()
    _setup_ran(result, calls)
    assert "node" not in result.stderr.lower(), result.stderr


def test_a_missing_jq_gets_a_warning_that_names_what_fails_without_it(box):
    # The git hooks bootstrap.sh arms run on git and bash alone, but the Claude Code safety hooks
    # in .claude/hooks/ read JSON with jq (pre-write-validate.sh blocks every write without it),
    # and make selftest's credential-check tests run those hooks. Setup still finishes.
    box.add_node()
    result, calls = box.run()
    _setup_ran(result, calls)
    warned = [line for line in result.stderr.lower().splitlines() if "jq" in line]
    assert warned, "no warning on stderr naming jq:\n" + result.stderr
    text = " ".join(warned)
    for consequence in (".claude/hooks", "make selftest"):
        assert consequence in text, f"the jq warning does not name {consequence}:\n{text}"


def test_with_jq_on_the_path_there_is_no_jq_warning(box):
    box.add_node()
    box.add_jq()
    result, calls = box.run()
    _setup_ran(result, calls)
    assert "jq" not in result.stderr.lower(), result.stderr
