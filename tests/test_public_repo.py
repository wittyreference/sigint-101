# ABOUTME: Guards the public repo: no git-tracked text file may hold a tailnet address or a path
# ABOUTME: into a real person's home directory. A finding names the file and line, never the text.

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# A dotted quad standing on its own, not a run inside a longer dotted number such as a five-part
# version. A full stop may still follow it, as at the end of a sentence.
DOTTED_QUAD = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\.?\d)")

# An absolute path into one person's home directory: /home/<user> (inside a WSL UNC path too),
# macOS /Users/<user>, and Windows <drive>:\Users\<user> with either slash, which also covers
# WSL's /mnt/c/Users/<user>. A placeholder such as <user>, $USER or {user} names nobody, so it
# does not match.
HOME_PATH = re.compile(r"(?:[\\/]home|[\\/]Users|\b[A-Za-z]:[\\/]+(?i:users))[\\/]+"
                       r"(?P<user>[A-Za-z0-9._-]+)")

# Made-up users whose home paths are test data, not anyone's machine:
#   learner - the kiosk account tests/test_kiosk.py builds its Chromium-profile fixtures on.
FIXTURE_USERS = frozenset({"learner"})

# The owner's name is not checked here: this file is public, so a test that searched for the
# name would have to contain it, which is the very leak it would guard against.


def tailnet_address_lines(text: str) -> list:
    """The 1-based numbers of the lines in `text` that hold a tailnet address.

    A tailnet hands out IPv4 addresses from RFC 6598's shared address space: first octet 100,
    second octet 64 to 127. A quad with an octet past 255 is no address at all.
    """
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        for match in DOTTED_QUAD.finditer(line):
            octets = [int(part) for part in match.groups()]
            if octets[0] == 100 and 64 <= octets[1] <= 127 and max(octets) <= 255:
                found.append(number)
                break
    return found


def foreign_home_lines(text: str) -> list:
    """The 1-based numbers of the lines in `text` that hold a path into a home whose user is not
    in FIXTURE_USERS. A full stop after the user name is read as the end of a sentence."""
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        users = [match.group("user").rstrip(".") for match in HOME_PATH.finditer(line)]
        if any(user and user not in FIXTURE_USERS for user in users):
            found.append(number)
    return found


def tracked_text_files(repo: Path = REPO) -> list:
    """(path, text) for every git-tracked text file in the checkout at `repo`. A tracked symlink
    counts as a one-line text: the path it points to, which is what git publishes for it.

    Deliberately not a skip when git cannot list them: a guard that checked nothing would still
    report success, the same trap tests/test_js_panels.py refuses for the panel tests.
    """
    git = shutil.which("git")
    assert git, ("git is not on PATH, so the tracked files cannot be listed and this guard would "
                 "check nothing. Install git, or say plainly that the privacy guard is not run.")
    listing = subprocess.run([git, "ls-files", "-s", "-z"], cwd=repo, capture_output=True,
                             timeout=60)
    assert listing.returncode == 0, ("git ls-files failed, so this guard would check nothing. Is "
                                     "this a git checkout? "
                                     + listing.stderr.decode(errors="replace"))
    entries = [entry for entry in listing.stdout.decode("utf-8").split("\0") if entry]
    assert entries, "git lists no tracked files here, so this guard would check nothing"
    files = []
    for entry in entries:
        info, path = entry.split("\t", 1)           # "<mode> <blob> <stage>", a tab, the path
        mode, blob, _stage = info.split(" ")
        if mode == "120000":            # a symlink: its blob is the path it points to
            target = subprocess.run([git, "cat-file", "-p", blob], cwd=repo, capture_output=True,
                                    timeout=60, check=True)
            files.append((path, target.stdout.decode("utf-8", errors="replace")))
            continue
        full = repo / path
        if not full.is_file():          # tracked but deleted in this checkout: nothing to read
            continue
        data = full.read_bytes()
        if b"\0" in data[:8000]:        # binary, by the first-8000-bytes test git itself uses
            continue
        files.append((path, data.decode("utf-8", errors="replace")))
    return files


def findings(lines_of, repo: Path = REPO) -> list:
    """`path:line` for every line `lines_of` flags in a tracked text file of the checkout at
    `repo`. The text itself never leaves this function, so a failure prints where, not what."""
    return [f"{path}:{number}" for path, text in tracked_text_files(repo)
            for number in lines_of(text)]


def test_no_tracked_file_holds_a_tailnet_address():
    found = findings(tailnet_address_lines)
    assert not found, ("tracked files hold a tailnet address; name the host instead. Where (the "
                       "addresses are withheld): " + ", ".join(found))


def test_no_tracked_file_holds_a_path_into_a_real_home():
    found = findings(foreign_home_lines)
    assert not found, ("tracked files hold a path into a real person's home directory; write "
                       "~/... or a named placeholder instead. Where (the paths are withheld): "
                       + ", ".join(found))


def test_without_git_the_guard_fails_loudly(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AssertionError, match="would check nothing"):
        tracked_text_files()


def test_outside_a_git_checkout_the_guard_fails_loudly(tmp_path):
    (tmp_path / "notes.md").write_text("anything\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="would check nothing"):
        tracked_text_files(tmp_path)


def test_a_tracked_symlink_into_a_real_home_is_found(tmp_path):
    # Git publishes a symlink as the path it points to, so a link into a home directory leaks that
    # path even when it dangles. The path is joined from parts, as the examples below are, and no
    # GIT_* variable of a calling hook may steer these commands at a real repository.
    (tmp_path / "plan.md").symlink_to("/home/" + "someone" + "/plans/plan.md")
    for command in (["git", "init", "-q"], ["git", "add", "plan.md"]):
        subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, timeout=60,
                       env={name: value for name, value in os.environ.items()
                            if not name.startswith("GIT_")})
    assert findings(foreign_home_lines, tmp_path) == ["plan.md:1"]     # where, never the path


# --- the patterns, proved on examples ---------------------------------------------------------
# Every example is joined from parts at run time, so this file never holds a whole address or a
# whole home path, and the guard reads it like any other tracked file.


def quad(*parts) -> str:
    """A dotted number joined from its parts."""
    return ".".join(str(part) for part in parts)


@pytest.mark.parametrize("line", [
    quad(100, 64, 0, 1),                                 # the first address of the block
    quad(100, 127, 255, 254),                            # the last
    f"ssh someone@{quad(100, 100, 7, 7)} 'uptime'",      # user@address
    f"--host {quad(100, 77, 1, 2)} --port 8008",
    f"run them against http://{quad(100, 81, 3, 4)}:8008/",
    f"The box answers at {quad(100, 99, 5, 6)}.",        # a full stop ends the sentence
    f"(`box`, {quad(100, 120, 9, 9)}, user `someone`)",
])
def test_a_tailnet_address_is_found(line):
    assert tailnet_address_lines(line) == [1]


@pytest.mark.parametrize("line", [
    quad(100, 63, 255, 255),                 # just below the block
    quad(100, 128, 0, 1),                    # just above it
    quad(10, 100, 64, 1),                    # a LAN address whose tail looks like the block
    quad(100, 64, 256, 1),                   # an octet past 255: no address at all
    "Chromium " + quad(100, 0, 4896, 127),   # a browser version that begins with 100
    "version " + quad(100, 64, 1),           # a three-part version
    "build " + quad(100, 64, 1, 2, 3),       # a five-part version
    quad(1, 100, 64, 0, 1),                  # an address-shaped run inside a longer number
    "dated " + quad(2026, 11, 16),           # a date
    "tune to 100.7 MHz",                     # a frequency
    "100_100_000",                           # a frequency in Hz, Python-style
])
def test_a_number_that_is_not_a_tailnet_address_is_left_alone(line):
    assert tailnet_address_lines(line) == []


@pytest.mark.parametrize("line", [
    "/home/" + "someone" + "/sigint-101",
    "cd /home/" + "someone",
    "the plan lives in /home/" + "someone" + ".",        # a full stop ends the sentence
    "/Users/" + "someone" + "/Library",
    "C:\\Users\\" + "someone" + "\\AppData",
    "C:/Users/" + "someone" + "/AppData",
    "c:\\users\\" + "someone",
    "/mnt/c/Users/" + "someone" + "/AppData",
    "\\\\wsl.localhost\\Ubuntu\\home\\" + "someone" + "\\work",
    "/home/learner/x then /home/" + "someone" + "/y",    # a real home beside a fixture one
])
def test_a_path_into_a_real_home_is_found(line):
    assert foreign_home_lines(line) == [1]


@pytest.mark.parametrize("line", [
    "/home/learner/.config/chromium-kiosk",      # the fixture user
    "HOME = Path('/home/learner')",
    "~/.claude/plans/plan.md",
    "ln -s <main checkout>/.venv <wt>/.venv",
    "/home/<user>/x",
    "/home/$USER/x",
    "/home/${USER}/x",
    "/home/{user}/x",
    "$HOME/.config/autostart",
    "/root/.ssh",
    "https://example.com/users/42",
    "C:\\Users\\<user>\\AppData",
])
def test_a_home_path_that_names_nobody_is_left_alone(line):
    assert foreign_home_lines(line) == []


def test_the_guard_does_not_trip_on_its_own_source():
    source = Path(__file__).read_text(encoding="utf-8")
    assert tailnet_address_lines(source) == []
    assert foreign_home_lines(source) == []
